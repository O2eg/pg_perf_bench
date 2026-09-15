# pg-perf-bench

## Overview

Based on [pg_perfbench](https://github.com/TantorLabs/pg_perfbench).

`pg-perf-bench` runs controlled PostgreSQL benchmarks and stores the result
together with the facts required to interpret it: the effective workload,
PostgreSQL configuration, server version, host properties, execution timing,
raw command output, and collection diagnostics.

The distribution and installed command are `pg-perf-bench`; the import package
and GitHub repository are named `pg_perf_bench`.

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
`pg-perf-bench` is invoked:

| Transport | Host facts and PostgreSQL lifecycle | `pgbench` / `psql` |
|---|---|---|
| `local` | local machine | local machine |
| `docker` | existing container | local machine through a published port |
| `ssh` | remote host | local machine through an SSH local-forwarding port |
| `--managed-pg-info FILE` | PostgreSQL protocol only; no host transport | local machine through the managed endpoint |

This separation keeps workload generation independent of target management and
makes the measured client location explicit.

Detailed operational guides are indexed in [doc/README.md](doc/README.md).

## Installation

Python 3.10 or newer is required. Create a virtual environment and install the package:

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
| `ARG_WORKLOAD_PATH` | bundled profile directory, or `--workload-path` |
| `ARG_WORKLOAD_SCALE` | `--workload-scale` |
| `ARG_WORKLOAD_DURATION_SECONDS` | `--workload-duration-seconds`, or the profile's `default_duration_seconds` |
| `ARG_PGBENCH_CLIENTS` | current client-axis value |
| `ARG_PGBENCH_TIME` | current duration-axis value |

Unresolved `ARG_*` placeholders fail before target mutation. Prefer
`PGPASSWORD` to placing `ARG_PG_PASSWORD` directly in a command line.

For a custom workload, use `--benchmark-type custom`, supply an existing
`--workload-path`, and reference files below that path from the command
templates.

### Bundled workload profiles

`pg-perf-bench profiles` lists the installed profiles, their default scale,
duration and script count. Select one with `--workload-profile`; its schema,
generator, setup and workload commands are supplied automatically.

| Profile | Workload and default script weights | Default duration |
|---|---|---:|
| [`imdb`](src/pg_perf_bench/workload_profiles/imdb/README.md) | 38 analytical scripts over 21 tables; equal weights | 120 s |
| [`pagila`](src/pg_perf_bench/workload_profiles/pagila/README.md) | OLTP: select / insert / update / delete = 50 / 25 / 20 / 5 | 60 s |
| [`pagila-htap`](src/pg_perf_bench/workload_profiles/pagila-htap/README.md) | The same OLTP scripts plus reporting: 50 / 25 / 20 / 5 / 5 | 60 s |

Weights describe selection of whole pgbench scripts, each of which can execute
several SQL statements or transactions. The default HTAP reporting share is
`5 / 105`, approximately 4.8 %. Its reported TPS includes all five scripts.

| Setting | How to configure it |
|---|---|
| Data volume | `--workload-scale SCALE`, default `1`; positive fractional values such as `0.25` are accepted. Generators retain minimum table sizes at small scales. |
| Concurrent clients | `--pgbench-clients 1,2,4,8,16`; each value gets a freshly recreated database. Bundled commands also use one pgbench job per client. |
| Measured window per point | `--workload-duration-seconds 120`; overrides the profile default for every client count. |
| Command time limit | `--command-timeout 300`; allow enough time for initialization and for the workload window plus completion of in-flight queries. |

Bundled profiles require `--pgbench-clients`; `--pgbench-time` is rejected.
They select benchmark type `custom` automatically. `--workload-path` cannot be
combined with `--workload-profile`.

For example, run the HTAP profile on a dedicated local benchmark instance
(adjust connection and PostgreSQL paths to your environment):

```bash
PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type local \
  --allow-database-reset \
  --host 127.0.0.1 --port 5432 --user postgres \
  --database pg_perf_bench_test \
  --pg-data-path /var/lib/postgresql/18/main \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --workload-profile pagila-htap \
  --workload-scale 4 \
  --workload-duration-seconds 120 \
  --pgbench-clients 1,2,4,8,16 \
  --command-timeout 300 \
  --report-name pagila-htap-scale4
```

Use `--workload-profile pagila` for the OLTP baseline, or `imdb` for analytical
joins. Keep scale, client counts and duration constant when comparing runs.
Scale controls row counts, not a target size in bytes: IMDb scale 1 generates
100,000 titles and 100,000 people; Pagila scale 1 generates 1,000 films,
600 customers and 16,000 rentals. Choose data volume relative to the cache
being tested; the profile READMEs describe the datasets in more detail.

The schemas support PostgreSQL 10–18. Generators use deterministic `hashint8()`
streams; IMDb uses separate streams for related identifiers and attributes to
avoid correlated, degenerate joins. Pagila initializes indexes, identifier
bounds and statistics, and sets `search_path` for the database and the benchmark
role within it. No manual role configuration is needed for the supplied commands.
Generated values are reproducible; service timestamps such as Pagila's
`last_update` use the current time.

Bundled commands use `--random-seed=42`, and both Pagila variants use
`-M prepared`. A fixed seed makes random choices repeatable with the same
client configuration, but a timed run can complete a different number of
scripts; it does not guarantee identical observed mix proportions or TPS.
These profiles use `profile.json`, independently of `pg_workload`'s
scheduler-specific `profile.yml`.

### Changing script weights or pgbench options

Keep `--workload-profile` and supply `--workload-command` to replace only its
measured command. The packaged initialization still runs, and
`ARG_WORKLOAD_PATH` still points to the packaged profile directory.
`--init-command` can similarly replace the initialization command.

For example, append this option to the HTAP command above to change the
reporting weight from 5 to 25, making its target share `25 / 125 = 20 %`:

```bash
--workload-command 'ARG_PGBENCH_PATH --no-vacuum --random-seed=42 -M prepared -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS -T ARG_WORKLOAD_DURATION_SECONDS -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER -f ARG_WORKLOAD_PATH/sql/01_select.sql@50 -f ARG_WORKLOAD_PATH/sql/02_insert.sql@25 -f ARG_WORKLOAD_PATH/sql/03_update.sql@20 -f ARG_WORKLOAD_PATH/sql/04_delete.sql@5 -f ARG_WORKLOAD_PATH/sql/05_reporting.sql@25 ARG_PG_DATABASE'
```

The replacement is a complete command: include every script you want to run.
Use `-f FILE@WEIGHT` for relative weights; omit a file to remove it from the mix.
Set pgbench options such as `--random-seed`, `-M` and `-j` in this command;
there are no separate profile CLI flags for them. Retain the client and duration
placeholders when those values should follow the benchmark settings.

### Editing SQL or the data generator

Copy the complete profile directory to the workload-generator host and edit
that copy. Using Python from the environment where `pg-perf-bench` is installed:

```bash
python3 - <<'PY'
from pathlib import Path
from shutil import copytree
import pg_perf_bench

source = Path(pg_perf_bench.__file__).parent / 'workload_profiles' / 'pagila-htap'
copytree(source, 'workloads/pagila-local')
PY
```

Edit `generator.py` to change data distributions. Adjust the probabilities in
`sql/02_insert.sql` to change the frequency of customer, catalogue and staff changes.
Edit the SQL scripts for query behavior. The copied `profile.json` contains
the command templates, including the script list and weights.

Use `--benchmark-type custom --workload-path` for the copy. A custom path does
not automatically apply commands or defaults from `profile.json`: pass both command templates explicitly,
and set the duration explicitly if they use `ARG_WORKLOAD_DURATION_SECONDS`.
The following reads your edited templates and runs them:

```bash
workload_init=$(python3 -c 'import json; print(json.load(open("workloads/pagila-local/profile.json"))["benchmark"]["init_command"])')
workload_run=$(python3 -c 'import json; print(json.load(open("workloads/pagila-local/profile.json"))["benchmark"]["workload_command"])')

PGPASSWORD=secret pg-perf-bench benchmark \
  --connection-type local \
  --allow-database-reset \
  --host 127.0.0.1 --port 5432 --user postgres \
  --database pg_perf_bench_test \
  --pg-data-path /var/lib/postgresql/18/main \
  --pg-bin-path /usr/lib/postgresql/18/bin \
  --benchmark-type custom \
  --workload-path ./workloads/pagila-local \
  --workload-scale 4 \
  --workload-duration-seconds 120 \
  --pgbench-clients 1,2,4,8,16 \
  --init-command "$workload_init" \
  --workload-command "$workload_run" \
  --command-timeout 300 \
  --report-name pagila-local-scale4
```

To validate a configured command without touching PostgreSQL, insert `plan`
before `benchmark`: `pg-perf-bench plan benchmark ...`.

### Profile files and parameters in reports

Both JSON and HTML reports retain the effective initialization and workload
commands for every iteration, together with scale, duration, client counts and
CLI arguments. This includes overridden script weights, seed, jobs and other
pgbench options. Passwords are redacted.

For bundled profiles, `workload_evidence.files` contains every file listed in
the manifest and `profile.json` itself. For a local profile, the report also
captures the other files under `--workload-path`, including JSON, YAML, TOML,
shell scripts and configuration files without extensions. The local manifest's
`files` entries supply file roles; they do not select commands or defaults.
Git/Mercurial/Subversion metadata, `.venv`, `venv`, `__pycache__`, `.pyc` and
`.pyo` are excluded from automatic traversal. Keep reports, generated datasets
and unrelated files outside the profile directory.

Literal input paths passed to psql or pgbench with `-f FILE`, `-fFILE`,
`--file FILE` or `--file=FILE` are captured too, including external SQL used by
an overridden command and pgbench's `FILE@WEIGHT` form. External files are
identified by absolute path in the report; prefer absolute paths in commands.
Relative file arguments are resolved from the directory where the benchmark
was launched. Dependencies opened inside SQL, Python or shell code should be
kept in the local profile directory: arbitrary shell expansion and runtime
dependency discovery are not performed.

Captured files must already exist, be UTF-8 text and be no larger than 5 MiB
each; symlinks are rejected. Their content and SHA-256 are stored in the report
and contribute to both definition and execution hashes. In HTML, open
**Workload initialization and configuration** for the manifest, schema,
generator and supporting files, and **pgbench workload** for workload SQL.

### Managed PostgreSQL

Pass `--managed-pg-info FILE` to benchmark an instance whose operating system
and PostgreSQL service are controlled by a cloud provider. The option selects
managed mode automatically; `--connection-type`, `--pg-data-path` and
`--pg-bin-path` are not required. The local `pgbench` and `psql` clients still
need to be installed.

The metadata file can have **any format**: JSON, YAML, plain text, PDF, an image
or another binary format. Its format is not parsed or used to configure the
connection. Include the provider, region, instance class, CPU/RAM, storage and
relevant service settings in it. The complete file, its name, size and SHA-256
are embedded as `managed_pg_info` in JSON and HTML. The file contents are displayed
directly as `plain_text` under **Managed PostgreSQL instance**. Binary content is
stored and displayed as Base64, with an explicit encoding note.
The file hash also participates in the execution plan and environment identity.

For example:

```bash
PGPASSWORD=secret PGSSLMODE=require pg-perf-bench benchmark \
  --managed-pg-info ./cloud-instance.yaml \
  --host db.example.cloud --port 5432 --user bench_owner \
  --database pg_perf_bench_test --allow-database-reset \
  --workload-profile pagila-htap --workload-scale 0.1 \
  --workload-duration-seconds 30 --pgbench-clients 1,2,4 \
  --command-timeout 120 --report-name managed-pagila
```

Use the TLS settings required by your provider; `PGSSLMODE` and `PGSSLROOTCERT`
apply to the database connections and local client tools.

Managed mode measures TPS, latency, transaction counts and client connection
time, retains the complete workload evidence, and collects PostgreSQL version,
settings and extensions through SQL using the supplied role. It recreates the
dedicated benchmark database before each iteration, so the role needs
`CREATEDB`, access to the `postgres` maintenance database and ownership of the
benchmark database. `--allow-database-reset` remains mandatory.

The utility does not restart the server, flush filesystems, drop OS caches,
install `postgresql.conf`, read server logs or run the OS sampler. Host facts,
OS metrics, `PostgreSQL pg_config` and server logs explicitly show
`No data. Managed PostgreSQL.` These expected limitations have status
`unsupported`; actual SQL or workload failures remain errors. Managed mode
cannot be combined with SSH/Docker transport, `--pg-custom-config` or
`--drop-os-caches`. `--collect-pg-logs` retains the unavailable-data marker.
Custom workload commands execute exactly as supplied and must themselves be
compatible with the provider's permissions.

### Iteration lifecycle

For each axis value in the regular mode without Patroni, the backend:

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

### Patroni

Benchmark mode automatically detects a running Patroni process on the selected
Linux database host (local, SSH, or Docker). It matches Patroni's `postgresql.data_dir`
to `--pg-data-path`, including symlinks. Merely installing `patronictl` does not
select this mode. The host account must be able to read the Patroni process's
`/proc` entries, environment and configuration; use the Patroni OS account or root.
When invoked as root, the probe switches to the data directory owner's account,
which also works in containers where root cannot read another user's process environment.

Patroni detection and control require **Python 3.10 or newer on the database host**,
including the Python environment of the running Patroni process. The detector
first checks `python3` in `PATH`, then accessible running Python executables.
If no compatible discovery interpreter is available, or Patroni itself uses an
older Python, the run stops with an explicit version error before any database
changes. A bare PostgreSQL container without Python can still use its normal
lifecycle when no Patroni markers are present.

The member helper uses the active process's Python environment and working directory.
It supports a positional YAML file, a configuration directory, and environment-only
configuration. The API address, authentication and TLS settings come from that
configuration using Patroni's own request client. The API need only be reachable
from the database host. Credentials are not copied into reports or command arguments.

For an API requiring mutual TLS, configure `ctl.certfile` and `ctl.keyfile` with
the client certificate and key. Server trust comes from `ctl.cacert` or
`restapi.cafile`. The server's `restapi.certfile`/`restapi.keyfile` are not used as
client credentials. All referenced files must be readable by the Patroni OS account.

Before each iteration, the utility checks that SQL reaches the detected primary,
drops the benchmark database, flushes filesystems, and requests a synchronous
[`POST /restart`](https://patroni.readthedocs.io/en/latest/rest_api.html#restart-endpoint)
on that member. It then waits for SQL access, verifies that the PostgreSQL start
time changed, and recreates the benchmark database. Patroni and the Docker
container remain running. Use a direct connection to the selected primary;
a SQL connection to another member is rejected.

API errors, timeouts, ambiguous detection, and Patroni data files without an
identifiable running Patroni process stop the benchmark. They never trigger a
fallback to `pg_ctl` or container stop/start. `--command-timeout` bounds remote
commands and API requests; the utility does not override Patroni's failover settings.

With Patroni, `--pg-custom-config` and `--drop-os-caches` are rejected before
changing the database or uploading a configuration. Apply PostgreSQL settings
through Patroni before the run. OS cache dropping requires PostgreSQL to remain
stopped, which the Patroni restart API does not provide. Managed PostgreSQL mode
continues to skip host lifecycle operations entirely.

The opt-in integration test provisions a separate `pg_stand` container, installs
Patroni and etcd, reproduces the `pg_ctl` race, and runs Docker, SSH, and local
benchmarks, including a wheel installation, environment-only Patroni configuration,
and an API protected by basic authentication and mutual TLS:

```bash
PG_STAND_BIN=/path/to/pg-stand \
PG_PERF_BENCH_PATRONI_INTEGRATION=1 \
python -m pytest -q -m integration tests/integration/test_pg_stand_patroni.py
```

The same test module checks rejection below Python 3.10 and discovery on Python
3.10 using locally installed `python:3.9-slim` and `python:3.10-slim` images.

## Transports

### Local

```bash
--connection-type local
```

Without Patroni, lifecycle commands use `pg_ctl` under the `postgres` account. Cache dropping
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

To use an identity already loaded into a local agent, replace `--ssh-key` with
`--ssh-agent`:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
ssh-add -l

pg-perf-bench collect-all-info \
  --connection-type ssh \
  --ssh-host db-host.example \
  --ssh-user postgres \
  --ssh-agent \
  --ssh-known-hosts /secure/path/known_hosts \
  --remote-pg-host 127.0.0.1 \
  --remote-pg-port 5432 \
  --host 127.0.0.1 \
  --port 55432 \
  --database appdb \
  --pg-bin-path /usr/lib/postgresql/18/bin
```

`--ssh-key` and `--ssh-agent` are mutually exclusive. Agent mode uses the live
socket inherited through `SSH_AUTH_SOCK`; the socket must remain available
until collection or benchmarking finishes. The utility does not start an
agent, run `ssh-add`, or forward the agent to the remote host. Both modes use
explicit public-key authentication and the selected `known_hosts` policy
without loading the user's OpenSSH configuration.

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
- parsed clients, duration, transaction count, average latency, latency standard
  deviation, failed/retried transaction percentages, initial connection time, and TPS;
- an explicit `maximum_tps` point with its axis value and complete metrics;
- separate charts for TPS, average transaction latency, transaction latency
  standard deviation, failed/retried transaction percentages and initial
  connection time, all using the selected client or duration axis;
- all `pg_diag` OS charts collected during every measured iteration: CPU
  utilization/load, RAM usage/pressure, disk throughput/IOPS/utilization/latency,
  and network throughput/packets;
- PostgreSQL version, available extensions, and server settings;
- replication policy, connected WAL senders, replication slots, WAL receiver,
  logical subscriptions, and persistent `synchronous_commit` overrides;
- host, kernel, CPU, memory, storage, network, and filesystem facts;
- item-level collection status and diagnostic reason;
- an optional PostgreSQL log archive reference backed by the report-local
  `db_logs/` directory.

Optional metrics are taken from the overall pgbench summary. Percentages retain
the values reported by pgbench. Retry statistics require `--max-tries` other than
1; latency standard deviation requires timing statistics (for example,
`--progress`); `--connect` reports average connection time instead of initial
connection time. Absent values remain `null`, appear as gaps, and display an
explicit no-data message when an entire chart is unavailable. A reported zero
remains a zero. Joined reports include a series per source for these charts.

The JSON and HTML files are written through temporary files and atomically
renamed into place. Report names cannot contain path separators, `.`/`..`, or a
NUL byte.

Render a JSON report again without rerunning a benchmark:

```bash
pg-perf-bench render \
  --from-json report/local-pg18.json \
  --out report/local-pg18.html
```

### Replication evidence

Benchmark, `collect-db-info`, and `collect-all-info` reports include a
**Replication** section. The queries follow the replication items in `pg_diag`
and support PostgreSQL 10–18.

| Item | Evidence |
| --- | --- |
| Replication mode | Primary/standby role, FIRST/ANY policy and required standby count, connected senders, synchronous/quorum senders, and current `SyncRep` waiters |
| Replication settings | WAL and replication settings, units, configuration source, and pending restart flags |
| Commit policy overrides | Database, role, and role-in-database defaults for `synchronous_commit` |
| WAL senders | Physical/logical consumers, associated slots, synchronous state, WAL positions, byte distances, and reported lag |
| Replication slots | Physical/logical slots, activity, WAL distance from `restart_lsn`, xmin horizons, and version-dependent validity/failover fields |
| WAL receiver | Upstream host/port, slot, receive/replay positions, and receiver timestamps on a standby |
| Logical subscriptions | Current database's subscriptions, enabled state, owner, publications, slots, worker count, and worker commit policy |

For benchmarks, this is a snapshot **after the workload iterations**, not a
replication time series. The collector's effective `synchronous_commit` and
persistent overrides provide configuration context; they cannot establish
settings changed inside workload sessions or individual transactions. Configured
synchronous standbys and actual connected senders are shown separately.

Empty items have an explicit explanation. Missing privileges produce a collection
error or restricted statistics, not a claim that replication is absent. Use a
role with `pg_read_all_stats` for complete sender and wait-event statistics;
subscription metadata additionally requires access to the listed `pg_subscription`
columns (restricted by default on PostgreSQL 10–13). Connection strings and
passwords are excluded from this section.

Fields unavailable on an older PostgreSQL version remain `null`. WAL distances
are differences between LSN positions, not disk usage measurements; distances
for different slots overlap and must not be summed.

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

Older `report-v1` artifacts without the Replication section can be joined with
new reports. Replication snapshots are displayed separately for each source;
an older source explicitly says that replication evidence was not collected.
Original column headers and collection statuses are retained. Required join-task
paths remain mandatory, including replication paths when explicitly selected.

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

Replication integration tests create and remove their own disposable containers.
They use locally installed `postgres:10` through `postgres:18` images to check SQL
compatibility and permissions, plus a real primary/standby pair to check
asynchronous, FIRST, and ANY replication and a complete benchmark:

```bash
PG_PERF_BENCH_REPLICATION_INTEGRATION=1 \
python -m pytest -m integration tests/integration/test_replication_report.py
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
