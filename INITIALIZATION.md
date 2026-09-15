# Common data initializer

`--init-mode auto` (default) selects the common loader for a profile with an
`initialization` entrypoint. All bundled profiles implement it. An explicit
`--init-command` selects the legacy command path; `--init-mode legacy` also
selects it. Arbitrary old commands remain supported without SQL rewriting.
`--init-mode fast` requires the common interface and rejects `--init-command`.

## Defaults and phases

| Option | Default | Meaning |
| --- | --- | --- |
| `--init-workers` | `4` | Maximum simultaneous data or index jobs; each owns its connection |
| `--init-batch-rows` | `100000` | Target rows per committed data batch |
| `--init-table-mode` | `unlogged` | Stored tables become UNLOGGED before data, then LOGGED afterward; `logged` skips conversion |
| `--init-fsync` | `off` | Temporarily disable fsync on the selected primary; `keep` preserves it |
| `--init-synchronous-commit` | `off` | Session setting in every loader connection; alternatives: `local`, `keep` |

The same scheduler runs bounded data batches and later builds indexes, including
unique indexes used for primary keys. It keeps at most `init-workers` jobs in
flight, regardless of dataset size. A batch commits independently; the loader
does not create one transaction for the entire dataset. Progress logs report
committed rows and batches per task.

Order for each fresh database:

1. Create the schema without indexes or foreign keys; make stored tables UNLOGGED.
2. Run data tasks in bounded batches, respecting their declared dependencies.
3. Reset sequences and complete any declared post-data steps.
4. Convert stored tables to LOGGED, one at a time to limit temporary rewrite space.
5. Build indexes in parallel using the same worker limit and scheduler.
6. Attach constraints and validate foreign keys, serially to avoid lock-order deadlocks.
7. Complete profile setup and run `VACUUM (FREEZE, ANALYZE)` on stored tables and
   materialized views, then `ANALYZE`.
8. Restore fsync, checkpoint and synchronize files on the database host.
9. Wait until directly connected physical replicas replay the initialization WAL.
10. Capture **Before workload** sizes and start pgbench with the original commit policy.

`--command-timeout` bounds each individual SQL job, table conversion, vacuum,
host command, and replica wait. It does not bound the sum of all data batches.
For large datasets, increase it to cover the longest table rewrite/index build
and the time replicas need to catch up. A timeout or failed phase aborts the run
before pgbench. Failed batches are not automatically retried; the next run
recreates the disposable database.

## Durability and replication

`fsync=off` affects **all databases on the selected primary** during preparation.
Use this default on disposable benchmark instances: an OS/server failure during
this interval can corrupt data, and UNLOGGED table contents are not crash safe.
Use `--init-fsync keep --init-table-mode logged` to preserve normal durability.
Disabling fsync requires a PostgreSQL superuser and local, Docker, or SSH access
to the database host. Managed endpoints must explicitly use `--init-fsync keep`.
Permissions and interrupted-run recovery are checked before the database reset.

The loader saves the original effective fsync value and the presence/value of
its `postgresql.auto.conf` override. It restores that exact override, reloads,
checkpoints and executes host `sync`, including on ordinary errors and
cancellation. A durable journal under
`~/.cache/pg_perf_bench/initialization/` allows the next run from the same user
and machine to restore the setting after a killed process. If the original
primary is unreachable, restoration fails visibly and the journal remains;
recovery requires access to that original server. There is no automatic resume
of partially loaded data or continuation after failover.

Pending recovery is checked even with `--init-fsync keep` or `--init-mode legacy`.
If any journal remains, a non-superuser SQL role stops before the database reset:
it cannot reliably identify the journal's server or restore its fsync override.
The error includes the journal directory. Recover interrupted runs with a
PostgreSQL superuser and access to the original database host before using a
restricted role again; the journals are retained until restoration succeeds.

Journal matching uses the cluster identifier, data directory, database-host name
and the directory's filesystem device/inode; a changed SQL IP address or port
does not hide a pending recovery. A changed host/directory identity blocks the
benchmark instead of adopting `fsync=off` as the original value. Older journals
without host identity require the original SQL address; if it has changed, the
run stops and reports the journal path for recovery on the original server.

Patroni's synchronous mode, DCS configuration and `synchronous_standby_names`
remain unchanged. `synchronous_commit=off` in loader sessions removes waits for
synchronous replica acknowledgements. It does **not** eliminate the WAL needed
to convert tables to LOGGED or build their indexes. UNLOGGED contents are not
sent to replicas during data generation; conversion rewrites each table and
generates its WAL. Budget disk space for a full extra copy of the largest table,
WAL and index-building temporary files. Conversion of a single table remains
one PostgreSQL operation even when data generation was batched.

Before pgbench, a fixed WAL position must be replayed by every replica seen
before the database reset/restart, at loader startup or at the barrier. The
pre-reset replica list is retained across restart. Replicas using physical slots
are identified by slot name, so reconnecting through a new IP address with the
same slot does not cause a false timeout. Without a slot, the identity is
`application_name` plus client address, including the number of connections with
that identity. Keep those identifiers stable during the run. A disconnected
replica causes a timeout. The target position is captured after preparation,
including VACUUM, fsync restoration and CHECKPOINT. Every required replica must reach or pass it:
no WAL from data preparation may remain unapplied when pgbench starts.
Cascaded replicas and logical subscribers are not covered by this
primary-side physical replay barrier. A restricted SQL role needs visibility of
replica statistics (`pg_read_all_stats` or `pg_monitor`) when replicas are connected.

Initialization options, per-phase durations, committed row/batch counts, final
fsync and the replay barrier are included in JSON and HTML reports. Options
affect the workload execution hash used for comparisons.

For repeatable size-based measurements on a disposable primary with two
synchronous replicas, see the [manual speed test](tests/benchmark/README.md).
It supports profile selection, target size, worker count and batch size, and
saves the full preparation time with a breakdown by phase.

## Profile interface

Add this section to `profile.json` and list the entrypoint in `files.generators`:

```json
"initialization": {
  "schema_version": "pg_perf_bench/load-plan-v1",
  "entrypoint": "generator.py"
}
```

The Python module exports `build_load_plan(scale) -> LoadPlan`. It describes
SQL and dependencies; the utility owns connections, transactions, batching,
concurrency, progress, temporary settings and phase ordering. It must not mutate
the database while building the plan.

```python
from pg_perf_bench.initialization import LoadPlan, LoadTask


def build_load_plan(scale):
    return LoadPlan(
        schemas=('bench',),
        schema_sql='CREATE SCHEMA bench; CREATE TABLE bench.events (id bigint, payload text)',
        data=(
            LoadTask(
                'events',
                'INSERT INTO bench.events SELECT g, md5(g::text) '
                'FROM generate_series($1::bigint, $2::bigint) g',
                count=max(1, round(1_000_000 * scale)),
            ),
        ),
        indexes=(LoadTask('events_pk', 'CREATE UNIQUE INDEX events_pk ON bench.events (id)'),),
        constraints=(
            LoadTask(
                'events_pk',
                'ALTER TABLE bench.events ADD PRIMARY KEY USING INDEX events_pk',
            ),
        ),
    )
```

`count=N` supplies inclusive `$1`, `$2` bounds from 1 through N, as bigint.
`count=None` is one bounded SQL job, useful for small dictionaries or an index.
`depends_on=('task_name',)` waits for all batches of those tasks in the same
phase. Task names must be unique within a phase; cycles are rejected before reset.
`max_rows_per_key` describes bounded fan-out (for example, seven actors per film)
so the scheduler reduces the key range accordingly. One key is indivisible;
if its fan-out exceeds the configured target, that batch can exceed the target.

Schema SQL must defer indexes and foreign keys. Partitioned parents have no
storage: only physical leaf tables are converted to UNLOGGED/LOGGED. Keep
materialized views empty until `finalize_sql`. Optional `prepare_sql`,
`after_data_sql` and `finalize_sql` run at their named phases; they must be
bounded setup operations, not a second unbounded data loader. Profiles must not
override loader settings or issue transaction control inside tasks.

Use deterministic identifiers and range-local SQL. Avoid `nextval()` ordering
dependencies, full source-table scans for every batch and offset pagination.
The bundled profiles show sequence reset, partitioned keys and derived datasets.
List every referenced SQL/JSON asset in `files` so reports embed and hash it.

For a copied/custom profile use `--workload-path DIR --benchmark-type custom`
and supply `--workload-command`. `--init-command` is optional when its manifest
provides this interface. Older profiles without it retain their original command
behavior; the fast loader does not try to split arbitrary SQL or shell scripts.
