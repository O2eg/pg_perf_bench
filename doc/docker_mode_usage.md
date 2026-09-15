# Docker transport

Use Docker transport when PostgreSQL runs in an existing container and the
workload generator runs on the Docker host. `pg_perf_bench` does not create
containers, pull images, or define storage. Provision disposable test
environments with `pg_stand` or another lifecycle tool.

## Execution boundary

| Operation | Location |
|---|---|
| PostgreSQL connection | published host port |
| `pgbench` and `psql` | Docker host |
| Host fact collectors | Docker host (unprivileged; PostgreSQL `pg_config` stays in container) |
| Timed `pg_diag` OS sampler | Docker host |
| PostgreSQL lifecycle | Patroni API inside the container when detected; otherwise container stop/start |
| Filesystem sync and optional cache drop | Docker host |

Because workload generation is external to the container, the report describes
the database target while pgbench latency also includes the published-port
network path.

## Prerequisites

- The container already exists and contains PostgreSQL.
- The current user has normal rootless-Docker or Docker-group access.
- PostgreSQL is published on `--host` and `--port`.
- The newest local `pgbench` and matching `psql` are installed. The runner
  selects them automatically and validates PostgreSQL server majors 10–18.
- `--pg-data-path` and `--pg-bin-path` are paths inside the container.

Never make `/var/run/docker.sock` world-writable.

## Read-only collection

```bash
PGPASSWORD=secret pg-perf-bench collect-all-info \
  --connection-type docker \
  --container-name pg-bench-18 \
  --host 127.0.0.1 \
  --port 55432 \
  --user postgres \
  --database postgres \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --report-name docker-facts
```

The container must already be running. Collection refuses to start a stopped
container and never replaces `postgresql.conf`.

## Benchmark

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type docker \
  --container-name pg-bench-18 \
  --allow-database-reset \
  --host 127.0.0.1 \
  --port 55432 \
  --user postgres \
  --database pg_perf_bench_test \
  --pg-data-path /var/lib/postgresql/18/docker \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --benchmark-type default \
  --pgbench-clients 1,4,16 \
  --init-command 'ARG_PGBENCH_PATH -i -s 10 -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --workload-command 'ARG_PGBENCH_PATH -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --command-timeout 120 \
  --report-name docker-pg18
```

Benchmark mode may start a stopped container because destructive target access
was explicitly confirmed. With `--reset-mode database` and no Patroni, it recreates
the selected database and stops/starts the container between iterations. Schema reset
keeps the running container and database in place. It does not delete the container or its
volumes.

For reproducible single-node targets, provision `configs/single.yaml` with
`pg_stand` and override `--pg-version 10` through `--pg-version 18`. Replicated
clusters are also supported: keep the intended replicas connected and record their
topology and acknowledgement policy. The common loader waits for direct physical
replicas to replay preparation WAL before pgbench starts. CPU, RAM, disk, and network charts
describe the Docker host that actually executes the PostgreSQL container.

## Fast loading in an existing database

The command above uses legacy `pgbench -i` initialization. Schema reset requires
an initialization profile with a common LoadPlan; adding only `--reset-mode schema`
to that command will fail. Pre-create a dedicated `pg_perf_bench_test` database,
ensure the SQL role has CREATE/schema ownership and replication-monitoring rights,
and use a bundled profile instead:

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type docker --container-name pg-bench-18 \
  --host 127.0.0.1 --port 55432 --user postgres \
  --pg-data-path /var/lib/postgresql/18/docker \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --database pg_perf_bench_test --allow-database-reset --reset-mode schema \
  --workload-profile pagila --workload-scale 1.15 --init-mode fast \
  --init-fsync keep --init-workers 4 --init-batch-rows 100000 \
  --workload-duration-seconds 5 --pgbench-clients 1,2 \
  --command-timeout 300 --report-name docker-pagila-schema
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

## Configuration, caches, and facts

[Patroni is detected automatically](../README.md#patroni) in database reset mode. With Patroni, only
PostgreSQL is restarted through the member's API; the container stays running.
The API need not have a published port. The container's `postgres` account must
be able to read Patroni's process environment and configuration. The two options
below are rejected with Patroni before changes.

Patroni's Python environment and the discovery interpreter inside the container
must be Python 3.10 or newer. See the linked Patroni section for mTLS client settings.

`--pg-custom-config FILE` copies a local file into the container and installs
it as `postgresql.conf` with PostgreSQL ownership. The old file is not restored
automatically.

`--drop-os-caches` affects the Docker host, not only the container. Use it only
on an isolated benchmark host.

Host fact collection does not depend on the PostgreSQL image's `lshw` or OS
package versions. Raw runtime interface addresses remain visible in the
report, while Docker bridge/veth names are excluded from the stable hardware
identity used by JOIN compatibility checks.

## Common failures

- container not found: verify `--container-name` and Docker context;
- connection refused: verify the published port and PostgreSQL listen rules;
- timeout exit 124/137: increase `--command-timeout` or inspect the command;
- partial hardware facts: use a diagnostic-capable image or accept the missing
  optional items.
