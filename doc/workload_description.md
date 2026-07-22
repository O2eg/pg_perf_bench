# Workload configuration

A workload consists of an initialization command, a measured command, and one
iteration axis. Both commands are trusted shell templates executed on the
workload-generator host.

## Iteration axis

Exactly one axis is required:

- `--pgbench-clients 1,4,16` supplies `ARG_PGBENCH_CLIENTS`;
- `--pgbench-time 10,30,60` supplies `ARG_PGBENCH_TIME`.

Values are comma-separated positive integers. The CLI does not automatically
add `-c`, `-T`, or any other pgbench flag; the command template decides how the
axis value is used.

Every axis value is a separate destructive iteration with a freshly recreated
database.

Bundled profiles intentionally use `--pgbench-clients`: they sweep concurrency
to find maximum TPS for one fixed dataset, query mix and environment.

## Placeholders

| Placeholder | Value source |
|---|---|
| `ARG_PG_HOST` | `--host` |
| `ARG_PG_PORT` | `--port` |
| `ARG_PG_USER` | `--user` |
| `ARG_PG_PASSWORD` | `--password` or `PGPASSWORD` |
| `ARG_PG_DATABASE` | `--database` |
| `ARG_PGBENCH_PATH` | newest installed local pgbench, or validated `--pgbench-path` |
| `ARG_PSQL_PATH` | same-major local psql, or validated `--psql-path` |
| `ARG_WORKLOAD_PATH` | `--workload-path` |
| `ARG_WORKLOAD_SCALE` | `--workload-scale` |
| `ARG_PGBENCH_CLIENTS` | current client-axis value |
| `ARG_PGBENCH_TIME` | current duration-axis value |

An unresolved `ARG_*` token fails before database mutation. Prefer
`PGPASSWORD` to embedding `ARG_PG_PASSWORD` in the command text.

## Standard pgbench workload

```bash
--benchmark-type default \
--pgbench-clients 1,4,16 \
--init-command 'ARG_PGBENCH_PATH -i -s 10 -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE' \
--workload-command 'ARG_PGBENCH_PATH -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER ARG_PG_DATABASE'
```

Keep the scale factor and all non-axis options constant when comparing axis
values.

## Custom SQL workload

```bash
--benchmark-type custom \
--workload-path /srv/workloads/orders \
--pgbench-clients 1,4,16 \
--init-command 'ARG_PSQL_PATH -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER -d ARG_PG_DATABASE -f ARG_WORKLOAD_PATH/schema.sql' \
--workload-command 'ARG_PGBENCH_PATH -h ARG_PG_HOST -p ARG_PG_PORT -U ARG_PG_USER -d ARG_PG_DATABASE -f ARG_WORKLOAD_PATH/read-write.sql -T 60 -c ARG_PGBENCH_CLIENTS -j ARG_PGBENCH_CLIENTS'
```

`--workload-path` must exist on the workload-generator host. The command is
responsible for referencing the required files.

## Bundled maximum-TPS profiles

`pg-perf-bench profiles` lists the packaged `imdb` analytical and `pagila`
mixed-OLTP profiles. Each includes a SQL schema, deterministic Python data
generator and pgbench query set. Select one without copying its command
templates:

```bash
pg-perf-bench benchmark \
  --workload-profile imdb \
  --workload-scale 1 \
  --pgbench-clients 1,2,4,8,16 \
  ...
```

The profiles do not use or copy `pg_workload`'s `profile.yml`. That file
describes continuous scheduling in `pg_workload`; `pg_perf_bench` instead
recreates the database for each concurrency point and measures maximum TPS.

## Command timeout

`--command-timeout` is a positive number of seconds applied independently to:

- every initialization command;
- every measured workload command;
- transport host commands unless an item defines a smaller timeout.

Choose a timeout above the workload duration and initialization budget. A
timeout terminates the local process group; Docker commands also run through an
in-container timeout wrapper.

## Recorded evidence

For every completed init and workload command the report records:

- redacted command text;
- return code;
- stdout and stderr;
- UTC start time;
- elapsed seconds;
- iteration index, axis name, and axis value;
- parsed pgbench clients, duration, transaction count, average latency,
  initial connection time, and TPS.

The top-level `workload_evidence` object also embeds the complete SQL schema,
setup and query files, full Python generator source, profile manifest, source
hashes, scale, command templates, exact resolved commands, pgbench/psql paths,
and client-axis values. Definition and execution hashes let JOIN scenarios
verify workload identity without relying on external files.

The report also records an explicit `maximum_tps` object containing the winning
axis value and its complete latency/transaction metrics. A joined report keeps
the per-source maxima in `joined_maximum_tps`.

If TPS cannot be parsed, raw output is retained and the result/chart items are
marked partial.

## Reproducibility checklist

- Use a dedicated disposable database and stable PostgreSQL configuration.
- Keep the workload generator, network path, scale factor, and non-axis flags
  constant across compared runs.
- Isolate the target from unrelated workloads.
- Set CPU frequency, power management, and storage-cache policy explicitly
  when those factors matter.
- Record whether OS caches were dropped.
- Do not infer statistical confidence from a single iteration per axis value.

Warm-up runs, automatic repetitions, confidence intervals, and continuous
target sampling are not implemented yet.
