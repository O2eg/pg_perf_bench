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
| Host fact collectors | inside the container as `postgres` |
| PostgreSQL lifecycle | container stop/start |
| Filesystem sync and optional cache drop | Docker host |

Because workload generation is external to the container, the report describes
the database target while pgbench latency also includes the published-port
network path.

## Prerequisites

- The container already exists and contains PostgreSQL.
- The current user has normal rootless-Docker or Docker-group access.
- PostgreSQL is published on `--pg-host` and `--pg-port`.
- Local `pgbench` and `psql` versions are compatible with the target.
- `--pg-data-path` and `--pg-bin-path` are paths inside the container.

Never make `/var/run/docker.sock` world-writable.

## Read-only collection

```bash
PGPASSWORD=secret pg-perf-bench collect-all-info \
  --connection-type docker \
  --container-name pg-bench-17 \
  --pg-host 127.0.0.1 \
  --pg-port 55432 \
  --pg-user postgres \
  --pg-database postgres \
  --pg-bin-path /usr/lib/postgresql/17/bin \
  --report-name docker-facts
```

The container must already be running. Collection refuses to start a stopped
container and never replaces `postgresql.conf`.

## Benchmark

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type docker \
  --container-name pg-bench-17 \
  --allow-database-reset \
  --pg-host 127.0.0.1 \
  --pg-port 55432 \
  --pg-user postgres \
  --pg-database pg_perf_bench_test \
  --pg-data-path /var/lib/postgresql/data \
  --pg-bin-path /usr/lib/postgresql/17/bin \
  --benchmark-type default \
  --pgbench-clients 1,4,16 \
  --pgbench-path /usr/bin/pgbench \
  --psql-path /usr/bin/psql \
  --init-command 'ARG_PGBENCH_PATH -i -s 10 -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --workload-command 'ARG_PGBENCH_PATH -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --command-timeout 120 \
  --report-name docker-pg17
```

Benchmark mode may start a stopped container because destructive target access
was explicitly confirmed. It recreates the selected database and stops/starts
the container between iterations. It does not delete the container or its
volumes.

## Configuration, caches, and facts

`--pg-custom-config FILE` copies a local file into the container and installs
it as `postgresql.conf` with PostgreSQL ownership. The old file is not restored
automatically.

`--drop-os-caches` affects the Docker host, not only the container. Use it only
on an isolated benchmark host.

Minimal PostgreSQL images often do not contain `sudo`, `lshw`, `ip`, or other
optional diagnostic utilities. Missing collectors produce a partial report;
they do not invalidate completed benchmark evidence.

## Common failures

- container not found: verify `--container-name` and Docker context;
- connection refused: verify the published port and PostgreSQL listen rules;
- timeout exit 124/137: increase `--command-timeout` or inspect the command;
- partial hardware facts: use a diagnostic-capable image or accept the missing
  optional items.
