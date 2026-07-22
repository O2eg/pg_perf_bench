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
| PostgreSQL lifecycle | container stop/start |
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
was explicitly confirmed. It recreates the selected database and stops/starts
the container between iterations. It does not delete the container or its
volumes.

For reproducible single-node targets, provision `configs/single.yaml` with
`pg_stand` and override `--pg-version 10` through `--pg-version 18`. Replication
must remain disabled for maximum-TPS runs. CPU, RAM, disk, and network charts
describe the Docker host that actually executes the PostgreSQL container.

## Configuration, caches, and facts

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
