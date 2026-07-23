# pg_perf_bench

## Overview

Based on [pg_perfbench](https://github.com/TantorLabs/pg_perfbench).

`pg_perf_bench` runs controlled PostgreSQL benchmarks and stores the result
together with the facts required to interpret it: the effective workload,
PostgreSQL configuration, server version, host properties, execution timing,
raw command output, and collection diagnostics.

Its benchmark question is maximum TPS for one workload profile and one complete
environment, including OS and PostgreSQL settings. `pg_workload` schedules and
runs workload profiles; `pg_perf_bench` sweeps load, resets the dataset for each
point, measures the saturation curve, and preserves the evidence needed for a
controlled comparison.

Every successful collection or benchmark produces two artifacts:

- a JSON document for automation and later comparison;
- a self-contained HTML report with embedded data, styles, ECharts,
  highlight.js, and third-party notices.

The HTML report has no runtime network dependency. Python 3.10 or newer is
required.

## What the utility does

The CLI supports three groups of workflows:

- `benchmark` recreates a dedicated database before every measured iteration,
  initializes the workload, runs it, and collects final host/database facts;
- `collect-sys-info`, `collect-db-info`, and `collect-all-info` gather evidence
  without running a workload;
- `join` validates the comparability of existing reports and builds one
  comparison report with combined tables, charts, logs, and benchmark evidence.

Additional commands render HTML, validate packaged content, expose component
capabilities, validate and summarize report artifacts, and build a deterministic
execution plan. The versioned [`pg_play` integration contract](doc/pg_play-integration.md)
documents the machine interface used by the orchestrator.

## Architecture

The backend is divided into explicit layers:

```text
CLI and automation contract
  -> typed configuration and validation
  -> benchmark / collection / join orchestration
  -> Local, Docker, or SSH transport
  -> PostgreSQL lifecycle and bounded process execution
  -> report item collectors
  -> atomic JSON and monolithic HTML persistence
```

The transport controls PostgreSQL and executes host fact collectors on the
selected target. The workload commands themselves run on the machine where
`pg_perf_bench` is invoked:

| Transport | Host facts and PostgreSQL lifecycle | `pgbench` / `psql` |
|---|---|---|
| `local` | local machine | local machine |
| `docker` | existing container | local machine through a published port |
| `ssh` | remote host | local machine through an SSH local-forwarding port |

This separation keeps workload generation independent of target management and
makes the measured client location explicit.

Detailed operational guides are indexed in [doc/README.md](doc/README.md).

## Installation

Create a virtual environment and install the package:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
.venv/bin/pg-perf-bench --version
```

For development:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ../pg_diag -e '.[dev]'
.venv/bin/ruff check src tests
.venv/bin/python -m pytest
```

The source checkout is needed until the minimum supported `pg_diag` release is
available from the configured package index. For a PyPI release, publish
`pg_diag` and `pg_stand` first; the tagged build intentionally verifies those
declared dependencies from the package index before publishing
`pg_perf_bench`.

`pgbench` and `psql` must be installed on the workload-generator host.
`pg_perf_bench` discovers every client below `/usr/lib/postgresql/*/bin` and in
`PATH`, selects the newest installed version, and refuses an explicitly selected
older client. This keeps the load generator on the current `pgbench` even when
the target is PostgreSQL 10–18. The target needs the PostgreSQL server utilities
required for lifecycle operations. `pg_diag` and host `iostat` provide the OS
sampling engine used during the workload window.

## CLI

```text
pg-perf-bench benchmark ...
pg-perf-bench collect-sys-info ...
pg-perf-bench collect-db-info ...
pg-perf-bench collect-all-info ...
pg-perf-bench join ...
pg-perf-bench render ...
pg-perf-bench validate-artifact REPORT.json
pg-perf-bench summarize REPORT.json
pg-perf-bench validate
pg-perf-bench profiles
pg-perf-bench join-tasks
pg-perf-bench plan ...
pg-perf-bench capabilities
```

Use `pg-perf-bench COMMAND --help` for the complete option list. The old
`--mode=COMMAND` form remains accepted as a compatibility adapter.

Common output options:

- `--report-name NAME` sets the safe base name of the JSON and HTML artifacts;
- `--out DIR` selects the artifact directory, default `report` (`--output-dir` is a compatibility alias);
- `--log-dir DIR` selects the application log directory, default `log`;
- `--log-level {info,debug,error}` sets verbosity;
- `--clear-logs` removes old `*.log` files from the selected log directory.

## Safety contract

Collection and benchmark modes have deliberately different mutation rules.

Collection:

- does not replace `postgresql.conf`;
- does not start or stop PostgreSQL;
- uses a read-only database session;
- applies a 10-second PostgreSQL statement timeout;
- records an item-level error and continues when an optional fact cannot be
  collected.

Benchmark:

- terminates sessions connected to the selected benchmark database;
- drops and recreates that database from `template0` before every iteration;
- refuses `postgres`, `template0`, and `template1`;
- requires the explicit `--allow-database-reset` confirmation;
- drops OS filesystem caches only when `--drop-os-caches` is supplied;
- accepts a replacement PostgreSQL configuration only in benchmark mode.

Use only a dedicated disposable database. Prefer a disposable environment
provisioned by `pg_stand` for development and integration tests.

Commands supplied through `--init-command` and `--workload-command` are trusted
shell input. Do not run workload definitions from an untrusted source.

## Passwords and SSH trust

Supply the PostgreSQL password through `PGPASSWORD` or `--password`. The
legacy `--pg-password` and `--pg-user-password` aliases are also accepted. Known secret fields and the
effective password value are redacted from logs, plans, reports, and command
evidence.

SSH host-key verification is enabled by default. Provide `--ssh-known-hosts`,
or use the normal `~/.ssh/known_hosts`. The
`--ssh-insecure-no-host-key-check` switch is intended only for isolated,
disposable stands.

## Collecting environment facts

Host-only collection does not require PostgreSQL connection options:

```bash
pg-perf-bench collect-sys-info \
  --connection-type local \
  --report-name host-facts
```

Database collection requires connection parameters and `--pg-bin-path` for
the packaged `pg_config` collector:

```bash
PGPASSWORD=secret pg-perf-bench collect-db-info \
  --connection-type local \
  --host 127.0.0.1 \
  --port 5432 \
  --user postgres \
  --database postgres \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --report-name db-facts
```

`collect-all-info` combines host and database facts. A missing optional tool or
permission does not discard valid data: the affected item becomes `error` or
`partial`, artifacts are still generated, and the CLI returns exit code 5.

Local and SSH hardware collectors invoke `sudo -n lshw`; Docker mode instead
collects host inventory without sudo and keeps the target container's
`pg_config` evidence separate. Raw interface state is retained, but runtime
Docker bridges do not participate in the stable JOIN hardware identity.

## Running a benchmark

Exactly one iteration axis is required:

- `--pgbench-clients 1,4,16` exposes each value as
  `ARG_PGBENCH_CLIENTS`;
- `--pgbench-time 10,30,60` exposes each value as `ARG_PGBENCH_TIME`.

The axis does not add pgbench options automatically; the workload command must
use the corresponding placeholder.

Example:

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

The command timeout applies independently to initialization and workload
commands. It must be longer than the expected command duration.

### Workload placeholders

| Placeholder | Value source |
|---|---|
| `ARG_PG_HOST` | `--host` |
| `ARG_PG_PORT` | `--port` |
| `ARG_PG_USER` | `--user` |
| `ARG_PG_PASSWORD` | `--password` or `PGPASSWORD` |
| `ARG_PG_DATABASE` | `--database` |
| `ARG_PGBENCH_PATH` | newest local pgbench, or validated `--pgbench-path` |
| `ARG_PSQL_PATH` | matching local psql, or validated `--psql-path` |
| `ARG_WORKLOAD_PATH` | `--workload-path` |
| `ARG_WORKLOAD_SCALE` | `--workload-scale` |
| `ARG_PGBENCH_CLIENTS` | current client-axis value |
| `ARG_PGBENCH_TIME` | current duration-axis value |

Unresolved `ARG_*` placeholders fail before target mutation. Prefer
`PGPASSWORD` to placing `ARG_PG_PASSWORD` directly in a command line.

For a custom workload, use `--benchmark-type custom`, supply an existing
`--workload-path`, and reference files below that path from the command
templates.

For a packaged maximum-TPS workload, use `--workload-profile imdb` or
`--workload-profile pagila`, `--workload-scale SCALE`, and a
`--pgbench-clients` sweep. `pg-perf-bench profiles` lists the installed
collection. Each profile supplies its schema, deterministic Python generator,
typical SQL query set and command templates. It deliberately does not reuse
`pg_workload`'s scheduler-specific `profile.yml`.

### Iteration lifecycle

For each axis value the backend:

1. verifies access to the PostgreSQL instance;
2. drops the dedicated benchmark database;
3. stops PostgreSQL or the selected container;
4. flushes filesystems and optionally drops host OS caches;
5. starts PostgreSQL and recreates the database;
6. runs the initialization command;
7. runs the workload command while the `pg_diag` Linux sampler records CPU,
   RAM, disk and network metrics on the database host;
8. stores raw stdout, stderr, return code, UTC start time, elapsed time, parsed
   pgbench metrics, and iteration metadata.

After the final iteration it collects the configured host and PostgreSQL facts
and optionally archives PostgreSQL logs under `<output-dir>/db_logs/`, alongside
the JSON and HTML report artifacts.

## Transports

### Local

```bash
--connection-type local
```

Lifecycle commands use `pg_ctl` under the `postgres` account. Cache dropping
requires a narrow non-interactive sudo rule for the specific command.

### Docker

```bash
--connection-type docker \
--container-name pg-bench-18
```

The container must already exist. Collection refuses to start a stopped
container. Benchmark mode may start it because target mutation was explicitly
confirmed. Use normal rootless-Docker or Docker-group access; never make the
Docker socket world-writable.

The workload reaches PostgreSQL through the port published on `--host` and
`--port`.

### SSH

```bash
--connection-type ssh \
--ssh-host db-host.example \
--ssh-port 22 \
--ssh-user postgres \
--ssh-key /secure/path/id_ed25519 \
--ssh-known-hosts /secure/path/known_hosts \
--remote-pg-host 127.0.0.1 \
--remote-pg-port 5432 \
--host 127.0.0.1 \
--port 55432
```

For database modes, `--host` and `--port` are the local bind address and
free port. `--remote-pg-host` and `--remote-pg-port` identify PostgreSQL from
the SSH server. Host commands run remotely; `asyncpg`, `pgbench`, and `psql`
connect through native AsyncSSH local forwarding. No `AcceptEnv` change is
required on the SSH server.

## Report contents

A benchmark report contains:

- artifact schema and generator versions;
- runtime and methodology metadata;
- redacted effective CLI configuration;
- workload templates and effective commands;
- complete embedded SQL schemas, queries and setup scripts;
- complete embedded Python generator source and profile manifest;
- workload file hashes, scale, pgbench/psql paths, client sweep and exact
  resolved commands;
- a compatibility preflight containing the PostgreSQL server major, the newest
  local pgbench/psql versions and the supported server range 10–18;
- raw initialization and workload evidence for every completed iteration;
- parsed clients, duration, transaction count, average latency, initial
  connection time, and TPS;
- an explicit `maximum_tps` point with its axis value and complete metrics;
- a TPS chart for the selected axis;
- all `pg_diag` OS charts collected during every measured iteration: CPU
  utilization/load, RAM usage/pressure, disk throughput/IOPS/utilization/latency,
  and network throughput/packets;
- PostgreSQL version, available extensions, and server settings;
- host, kernel, CPU, memory, storage, network, and filesystem facts;
- item-level collection status and diagnostic reason;
- an optional PostgreSQL log archive reference backed by the report-local
  `db_logs/` directory.

The JSON and HTML files are written through temporary files and atomically
renamed into place. Report names cannot contain path separators, `.`/`..`, or a
NUL byte.

Render a JSON report again without rerunning a benchmark:

```bash
pg-perf-bench render \
  --from-json report/local-pg18.json \
  --out report/local-pg18.html
```

## Joining reports

Join mode requires at least two benchmark reports with:

- unique internal `report_name` values;
- the same `artifact_schema_version`;
- complete benchmark chart and result-table structures;
- equal values at every dotted path listed by the selected join task.

The explicitly selected reference remains immutable while every other report
is compared with it. Non-required differences become report/value comparison
tables. TPS chart series, pgbench result tables, log references, and raw
`benchmark_runs` evidence are deep-copied into the joined artifact. OS chart
blocks are intentionally stacked vertically by source report and iteration;
CPU profiles therefore remain visually comparable instead of being overlaid.

```bash
pg-perf-bench join \
  --input-dir report/runs \
  --reference-report local-pg18.json \
  --join-task optimize-db-config \
  --out report/comparisons \
  --report-name clients-comparison
```

The input directory should contain only source JSON reports intended for that
comparison. Invalid non-reference JSON files are skipped with a warning. A
missing, invalid, or structurally incompatible reference fails the operation.
`pg-perf-bench join-tasks` lists the packaged JOIN catalog of separately
documented practical scenarios:
`optimize-db-config`, `scale-cpu`, `scale-memory`, `compare-storage`,
`tune-os-kernel`, `compare-postgresql-major`, and `repeatability`. Each scenario
fixes the evidence required by its performance question and permits only its
declared variable to differ. Definitions and README files are validated by
`pg-perf-bench validate`. The historic
`task_compare_dbs_on_single_host.json` name remains an alias for
`optimize-db-config`.

## Automation contract

`--machine` emits one JSON envelope on stdout and sends logs to stderr. It may
appear before or after the subcommand. `--request-id` is copied to the envelope.

```bash
pg-perf-bench --machine --request-id run-42 capabilities
pg-perf-bench --machine --request-id capabilities-42 --component-capabilities
pg-perf-bench --machine validate
pg-perf-bench --machine plan collect-sys-info --connection-type local
```

`plan` validates and redacts a configuration, then produces a deterministic
SHA-256 plan hash without touching the target. A machine-mode `benchmark` must
carry that reviewed hash. The hash includes custom workload file or directory
content, but excludes output paths, log settings, report name, and request id:

```bash
pg-perf-bench --machine plan benchmark BENCHMARK_OPTIONS...
pg-perf-bench --machine benchmark BENCHMARK_OPTIONS... --plan-hash sha256:...
```

All component capabilities use `pg_play/capabilities/v1`; every command declares
`mutates_target`, `machine_output`, and `accepts_plan_hash`. Generated artifacts
carry an absolute path, SHA-256 hash, size, kind, and schema version.

Stable exit codes:

| Code | Meaning |
|---:|---|
| 0 | success |
| 2 | invalid CLI or configuration |
| 3 | missing precondition or inaccessible dependency |
| 4 | unsupported operation |
| 5 | report generated with partial collection results |
| 6 | execution failure |
| 7 | cancelled operation |
| 8 | ownership error |
| 130 | interrupted by the user |

## Validation and tests

Validate the installed templates, command references, Python collectors, and
join task definitions:

```bash
pg-perf-bench validate
```

Run the non-destructive test suite:

```bash
python -m pytest
```

Integration tests are excluded by default. The supported end-to-end smoke test
uses an explicitly provisioned disposable `pg_stand` environment:

```bash
PG_PERF_BENCH_PG_STAND_INTEGRATION=1 \
python -m pytest -m integration tests/integration/test_pg_stand_smoke.py
```

The legacy direct-Docker integration module is disabled unless
`PG_PERF_BENCH_LEGACY_DOCKER_INTEGRATION=1` is set.

## Current scope

The current report captures static environment facts, final PostgreSQL state,
and continuous host CPU, RAM, disk, and network time series during every
measured workload iteration. Continuous PostgreSQL wait-event and `pg_stat_*`
time-series sampling is not yet part of the report. Statistical repetitions,
warm-up runs, and confidence intervals remain outside the current execution
model.

## License

The project is distributed under the MIT License. Embedded third-party assets
retain their own license and notice files under
`src/pg_perf_bench/templates/vendor/` and in `THIRD_PARTY_NOTICES.md`.
