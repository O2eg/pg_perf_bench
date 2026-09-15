# Managed PostgreSQL

`--managed` explicitly selects SQL-only operation. The utility does not require
host paths or SSH/Docker access and does not restart PostgreSQL, change server
configuration, flush filesystems or drop OS caches. `--managed-pg-info FILE` is
optional metadata; supplying it also enables managed mode for compatibility.

## Existing database workflow

Create a dedicated benchmark database through the provider interface, for example
`perf_check` owned by `user1`. Give the benchmark role CREATE privilege on that
database and ownership of any existing profile schemas. Use the primary endpoint
and direct connections or compatible session pooling (see the requirements below). Transaction/statement pooling is not
supported; the utility cannot reliably detect every external pooler's mode.

```bash
PGSSLMODE=verify-full PGSSLROOTCERT=/path/to/provider-ca.pem \
pg-perf-bench benchmark \
  --managed --host primary.example --port 6432 --user user1 \
  --database perf_check --allow-database-reset --reset-mode schema \
  --init-fsync keep --workload-profile pagila --workload-scale 1.15 \
  --workload-duration-seconds 5 --pgbench-clients 1,2 \
  --command-timeout 300 --report-name managed-pagila
```

Supply credentials through the usual environment/passfile mechanism. Local
`pgbench` and `psql` clients are required. Add `--managed-pg-info instance.yaml`
to embed provider, CPU/RAM, storage, pooling and replication details in the report.

Schema reset supports all bundled profiles and custom profiles exposing a common
LoadPlan. Arbitrary legacy init commands require database reset. The default
reset mode is still `database`; that mode requires CREATEDB, database ownership
and access to `postgres`. Schema reset uses only the selected database and
requires `--init-fsync keep`, including on self-managed servers.

The loader retains batching, parallel index construction and UNLOGGED→LOGGED.
`--init-table-mode logged` skips the conversion. The default
`--init-synchronous-commit keep` does not change the session commit policy.
Explicit `--init-synchronous-commit off` removes local WAL flush and synchronous
replica acknowledgement waits during preparation. `local` keeps local WAL flush
waits but removes synchronous replica acknowledgement waits. These SQL session
overrides are verified and restored before returning loader connections to a pool.
pgbench runs with its original commit policy. Its search_path is set for its own
connections; permanent database/role search_path values are preserved.

Replication monitoring and WAL-function permissions are checked before reset.
Every required direct physical replica must replay a fixed WAL position captured
after preparation before pgbench starts. On MDB, request equivalent monitoring
privileges through the cloud interface; see [MDB roles](https://yandex.cloud/ru/docs/managed-postgresql/concepts/roles).
Hidden replication statistics do not count as zero lag. Logical and cascaded
replication are outside this primary-side barrier.

## Connection pooling

Use a direct primary endpoint or session pooling. With Odyssey, configure:

```text
# Global setting:
smart_search_path_enquoting yes

# Inside the benchmark database/user route:
pool "session"
pool_discard yes
pool_rollback yes
```

`smart_search_path_enquoting` preserves multi-schema `search_path` supplied by
libpq clients. `pool_discard` cleans server prepared statements when a client
leaves; pgbench reuses statement names in subsequent processes. See the Odyssey
[global settings](https://github.com/yandex/odyssey/blob/master/docs/configuration/global.md)
and [route settings](https://github.com/yandex/odyssey/blob/master/docs/configuration/rules.md).
If the provider does not expose these settings, request a compatible endpoint or
use a direct primary connection. Transaction/statement pooling is unsupported.

Before schema reset, psql checks the actual startup `search_path`, including
schemas that do not exist yet. Two read-only pgbench connection probes check
prepared-statement reuse. These probes run `SELECT 1`, outside the measured workload.
Incompatible settings fail before deleting existing schemas. This checks common
incompatibilities; it is not automatic identification of every pooler or its mode.
No workload SQL runs until initialization and the physical replay barrier finish.
Allow enough server connections for the controller and preparation connection plus
all loader workers, and later for the controller plus all pgbench clients. A pool
smaller than the selected concurrency can serialize clients or cause timeouts.

The loader applies settings through SQL, so it does not depend on Odyssey
forwarding arbitrary startup parameters. It restores its session overrides and
explicitly releases its advisory lock. Diagnostic connections clear read-only
mode and timeouts before returning to the pool. Correct pool cleanup is still
required for pgbench and other clients using the same route.

## Reports and retries

Every iteration keeps initialization evidence, Before/After sizes, metrics and
raw pgbench output in JSON/HTML. Reset mode is recorded in report parameters,
methodology and the workload execution hash. A run without instance metadata marks
the managed instance identity as unknown instead of inventing hardware information.

Unavailable optional SQL diagnostics, such as other database sizes or subscription
details, remain explicit. The benchmark can complete with a partial report and
CLI exit code **5**; inspect `collection_summary` and individual reasons. Host
diagnostics are marked unsupported. Reset, initialization, replay-barrier and
workload failures still abort the benchmark.

The final dataset remains for inspection. A repeated command resets the profile
schemas and reloads data before each iteration. Use a dedicated database without
application dependencies: `DROP SCHEMA ... CASCADE` can remove dependent objects
in other schemas. Schema reset is not a sandbox for arbitrary custom profile SQL.


## Compare managed PostgreSQL and Patroni

The following workflow compares a managed primary with a Patroni primary, each
with **one or more directly connected physical replicas**. Pre-create a dedicated
`perf_check` database on both clusters and provide CREATE/schema ownership and
replication-monitoring privileges to their benchmark users. Connect to the current
primary, not to a read-replica endpoint. Keep all intended replicas connected before
starting each command; the barrier tracks replicas visible at preflight and
preparation, and cannot discover an offline member of a provider's control plane.

For a comparable preparation method, use `--managed --reset-mode schema --init-fsync keep`
on **both** endpoints. `--managed` selects SQL-only access and can also be used
against self-managed Patroni: these runs do not restart servers or collect host
metrics. Patroni continues controlling its cluster; its DCS, synchronous mode and
`synchronous_standby_names` are unchanged. SQL replication evidence is collected
on both sides. This workflow avoids comparing one freshly restarted server with
another server whose caches survived preparation.

Before each pair, record and, when they are controlled inputs, match:

| Dimension | Check on both clusters |
| --- | --- |
| Replicas | Count, application names/slots, direct versus cascaded topology |
| Acknowledgements | Async or sync, FIRST/ANY, number of required acknowledgements |
| Commit policy | Effective workload `synchronous_commit`, including role/database overrides |
| Other inputs | PostgreSQL version, CPU/RAM, storage limits, connection pool, network placement |

For example, compare primary + one synchronous replica against the same topology;
then run a separate pair with primary + two replicas on each side. Two replicas
with `ANY 1` have a different acknowledgement requirement from `ANY 2` or `FIRST 2`.
If replica count or policy intentionally differs, label the result accordingly:
its performance effect is part of the deployment comparison.

Use the same load-generator host, utility/client versions, workload sources,
scale, duration and client counts. Save provider/Patroni topology and resource
details into `managed-instance.txt` and `patroni-instance.txt`. Metadata is opaque
report evidence; it does not configure replication. Supply metadata for both
reports or omit it from both so their optional item structures match.

```bash
mkdir -p report/paired
common=(
  --managed --database perf_check --allow-database-reset --reset-mode schema
  --init-mode fast --init-fsync keep --init-table-mode unlogged
  --init-workers 4 --init-batch-rows 100000
  --workload-profile pagila --workload-scale 300
  --workload-duration-seconds 180 --pgbench-clients 16,64,128
  --command-timeout 3600 --output-dir report/paired
)

# Optional acceleration during preparation, applied equally to both runs:
# common+=(--init-synchronous-commit off)
# Without it, loader synchronous_commit is unchanged (keep).

PGPASSFILE=/secure/benchmark.pgpass \
PGSSLMODE=verify-full PGSSLROOTCERT=/secure/managed-ca.pem \
pg-perf-bench benchmark "${common[@]}" \
  --host managed-primary.example --port 6432 --user user1 \
  --managed-pg-info managed-instance.txt --report-name managed-pagila

PGPASSFILE=/secure/benchmark.pgpass \
PGSSLMODE=verify-full PGSSLROOTCERT=/secure/patroni-ca.pem \
pg-perf-bench benchmark "${common[@]}" \
  --host patroni-primary.example --port 5432 --user bench_owner \
  --managed-pg-info patroni-instance.txt --report-name patroni-pagila

pg-perf-bench join \
  --input-dir report/paired --reference-report managed-pagila.json \
  --join-task compare-deployments \
  --out report/comparisons --report-name managed-vs-patroni
```

Adjust endpoint ports and certificates to the actual deployment. Increase the
command timeout for large table rewrites, index builds or slow replica replay.
Scale is a profile multiplier, not a size in GiB; inspect the Before workload
sizes to compare actual datasets. For a quick workflow check, use scale `1.15`,
duration `5` and clients `1,2` on both sides. Repeat with `--workload-profile imdb`
and a suitable scale for a separate IMDb comparison; use a separate input directory
and reference/report names for every pair.

The [compare-deployments scenario](../src/pg_perf_bench/join_tasks/compare-deployments/README.md)
checks identical workload execution hashes, client tools, reset/restart and cache
policies. Deployment settings and hardware may differ. It does not prove matching
replication policies; inspect the Replication items, initialization barrier,
commit overrides, metadata and Before/After sizes. Missing SQL diagnostics remain
explicit, including exit code 5 for a partial report. In scripts using `set -e`,
handle that code explicitly and inspect the report before attempting JOIN.
