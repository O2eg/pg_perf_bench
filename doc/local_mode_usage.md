# Local transport

Use the local transport when PostgreSQL, host fact collection, and the workload
generator all run on the same machine.

## Execution boundary

| Operation | Location |
|---|---|
| PostgreSQL connection | local host |
| `pgbench` and `psql` | local host |
| Host fact collectors | local host |
| PostgreSQL lifecycle | local Patroni API when detected; otherwise `pg_ctl` as `postgres` |
| Filesystem sync and optional cache drop | local host |

## Prerequisites

- `pgbench` and `psql` are installed locally.
- `--pg-bin-path` contains `pg_ctl` and `pg_config`.
- `--pg-data-path` points to the disposable cluster being tested.
- For database reset without Patroni, the invoking account can execute `su - postgres`
  non-interactively for lifecycle operations. In a conventional installation
  this normally means running the benchmark workflow as root in an isolated stand.
- PostgreSQL is reachable through `--host` and `--port`.

Collection does not use `pg_ctl` and does not require database-reset
confirmation.

## Read-only collection

Collect host facts only:

```bash
pg-perf-bench collect-sys-info \
  --connection-type local \
  --report-name local-host
```

Collect host and database facts:

```bash
PGPASSWORD=secret pg-perf-bench collect-all-info \
  --connection-type local \
  --host 127.0.0.1 \
  --port 5432 \
  --user postgres \
  --database postgres \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --report-name local-facts
```

Collection uses a read-only database session and does not stop PostgreSQL or
replace its configuration.

## Benchmark

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type local \
  --allow-database-reset \
  --host 127.0.0.1 \
  --port 5432 \
  --user postgres \
  --database pg_perf_bench_test \
  --pg-data-path /var/lib/postgresql/18/main \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --benchmark-type default \
  --pgbench-clients 1,4,16 \
  --init-command 'ARG_PGBENCH_PATH -i -s 10 -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --workload-command 'ARG_PGBENCH_PATH -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --command-timeout 120 \
  --report-name local-pg18
```

With the default `--reset-mode database`, each iteration recreates the selected database.
Schema reset is shown in the next example. System databases are always rejected, but every other database is treated as
disposable after `--allow-database-reset` is supplied.

## Fast loading in an existing database

The command above uses legacy `pgbench -i` initialization. Schema reset requires
an initialization profile with a common LoadPlan; adding only `--reset-mode schema`
to that command will fail. Pre-create a dedicated `pg_perf_bench_test` database,
ensure the SQL role has CREATE/schema ownership and replication-monitoring rights,
and use a bundled profile instead:

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type local \
  --host 127.0.0.1 --port 5432 --user postgres \
  --pg-data-path /var/lib/postgresql/18/main \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --database pg_perf_bench_test --allow-database-reset --reset-mode schema \
  --workload-profile pagila --workload-scale 1.15 --init-mode fast \
  --init-fsync keep --init-workers 4 --init-batch-rows 100000 \
  --workload-duration-seconds 5 --pgbench-clients 1,2 \
  --command-timeout 300 --report-name local-pagila-schema
```

This loads in batches, converts UNLOGGED tables to LOGGED, builds indexes in
parallel and waits for direct physical replicas to replay preparation WAL.
The database and server are not restarted. Server paths remain required for this
transport's evidence collection. Use `--reset-mode database` for a database
recreation/restart run; that mode also requires CREATEDB and access to `postgres`.

The default protocol is simple. Add `--pgbench-prepared` to this profile command
for prepared statements. `--init-synchronous-commit keep` is the default; add
`--init-synchronous-commit off` only when deliberately skipping acknowledgement
waits during loading. The workload's commit policy and the replay barrier remain
unchanged. For larger runs, increase scale, duration, clients and timeout together.
For SQL-only comparisons with managed PostgreSQL, follow the
[paired managed/Patroni example](managed_mode_usage.md#compare-managed-postgresql-and-patroni).

## Configuration and cache handling

[Patroni is detected automatically](../README.md#patroni) in database reset mode. For a Patroni-managed
primary, PostgreSQL is restarted through that member's API. The local account
must be able to read Patroni's process environment and configuration. The two
options below are rejected with Patroni before any database changes.

Patroni's Python environment and the discovery interpreter must be Python 3.10
or newer. See the linked Patroni section for mTLS client settings.

`--pg-custom-config FILE` atomically installs the supplied file as
`postgresql.conf` before the reset sequence. Use this only for a disposable
cluster; the previous configuration is not restored automatically.

`--drop-os-caches` runs a host-wide cache drop and therefore affects unrelated
workloads on the same machine. It is disabled by default and requires a narrow
non-interactive sudo rule.

## Common failures

- `su: Authentication failure`: the invoking account cannot control PostgreSQL
  as `postgres`; use an isolated stand with the necessary privilege.
- `pg_ctl: not found`: correct `--pg-bin-path`.
- connection timeout after restart: verify the cluster paths, port, and logs.
- partial `lshw` items: install `lshw` and configure the required `sudo -n`
  access, or accept those optional items as unavailable.
