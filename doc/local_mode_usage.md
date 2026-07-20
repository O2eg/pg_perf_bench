# Local transport

Use the local transport when PostgreSQL, host fact collection, and the workload
generator all run on the same machine.

## Execution boundary

| Operation | Location |
|---|---|
| PostgreSQL connection | local host |
| `pgbench` and `psql` | local host |
| Host fact collectors | local host |
| `pg_ctl` lifecycle | local host, under the `postgres` account |
| Filesystem sync and optional cache drop | local host |

## Prerequisites

- `pgbench` and `psql` are installed locally.
- `--pg-bin-path` contains `pg_ctl` and `pg_config`.
- `--pg-data-path` points to the disposable cluster being tested.
- The invoking account can execute `su - postgres` non-interactively for
  lifecycle operations. In a conventional installation this normally means
  running the benchmark workflow as root in an isolated stand.
- PostgreSQL is reachable through `--pg-host` and `--pg-port`.

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
  --pg-host 127.0.0.1 \
  --pg-port 5432 \
  --pg-user postgres \
  --pg-database postgres \
  --pg-bin-path /usr/lib/postgresql/17/bin \
  --report-name local-facts
```

Collection uses a read-only database session and does not stop PostgreSQL or
replace its configuration.

## Benchmark

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type local \
  --allow-database-reset \
  --pg-host 127.0.0.1 \
  --pg-port 5432 \
  --pg-user postgres \
  --pg-database pg_perf_bench_test \
  --pg-data-path /var/lib/postgresql/17/main \
  --pg-bin-path /usr/lib/postgresql/17/bin \
  --benchmark-type default \
  --pgbench-clients 1,4,16 \
  --pgbench-path /usr/bin/pgbench \
  --psql-path /usr/bin/psql \
  --init-command 'ARG_PGBENCH_PATH -i -s 10 -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --workload-command 'ARG_PGBENCH_PATH -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
  --command-timeout 120 \
  --report-name local-pg17
```

For every iteration the selected database is dropped and recreated. System
databases are always rejected, but every other database is treated as
disposable after `--allow-database-reset` is supplied.

## Configuration and cache handling

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
