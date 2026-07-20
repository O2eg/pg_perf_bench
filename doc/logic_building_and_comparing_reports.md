# Reports and comparisons

Operational commands produce a machine-readable JSON artifact and a
self-contained HTML presentation with the same safe base name.

## Persistence model

Each artifact is written to a temporary file, flushed, and atomically renamed
into place. JSON and HTML are individually atomic; they are not a two-file
transaction.

`--report-name` is a base name, not a path. Path separators, `.`/`..`, empty
names, and NUL bytes are rejected. Select the destination with `--output-dir`.

## Report metadata

New reports include:

- `artifact_schema_version` for compatibility checks;
- generator name and version;
- Python and platform runtime information;
- report name and collection timestamp;
- collection summary and item-level statuses.

A benchmark report additionally contains methodology flags and raw
`benchmark_runs` evidence.

## Item status

Every template item is independent:

| Status | Meaning |
|---|---|
| `ok` | collector completed with usable data |
| `empty` | collector completed but returned no rows or text |
| `partial` | some data is usable but part of the item is missing |
| `skipped` | required execution context, such as a DB connection, was absent |
| `error` | collector failed or exceeded its timeout |

An optional item failure does not discard successful facts. The report is
persisted with a partial summary and CLI exit code 5.

## JSON and HTML roles

Use JSON for automation, validation, archival, and joining. Treat it as the
source artifact.

HTML embeds the JSON payload, ECharts, highlight.js, styles, and third-party
notices. It can be opened without network access. Re-render an existing JSON
artifact with:

```bash
pg-perf-bench render \
  --from-json report/run.json \
  --out report/run.html
```

## Join task

A join task lists dotted report paths that must exist and be equal in every
source report:

```json
{
  "description": "Compare runs from one stable environment",
  "items": [
    "sections.system.reports.cpu_info.data",
    "sections.db.reports.version_major.data",
    "sections.db.reports.pg_settings.data"
  ]
}
```

The CLI accepts the file name of a packaged join task. Run
`pg-perf-bench validate` to verify the packaged task definitions.

Choose comparison items that define the controlled environment. Do not include
the measured result itself, such as TPS, because that value is expected to
differ.

## Join preconditions

Join requires at least two source reports with:

- unique internal `report_name` values;
- the same `artifact_schema_version`;
- complete chart and pgbench-result structures;
- every required join-task path present and equal.

The input directory should contain only the source JSON files intended for the
comparison. Invalid non-reference JSON files are skipped with a warning. The
operation fails if the explicit reference is missing, invalid, or incompatible.

## Join execution

```bash
pg-perf-bench join \
  --input-dir report/runs \
  --reference-report run-a.json \
  --join-tasks task_compare_dbs_on_single_host.json \
  --output-dir report/comparisons \
  --report-name comparison
```

The selected reference is never mutated while comparisons are being made.
Every candidate is compared with the original reference, not with an
incrementally modified result.

The joined artifact contains:

- `join_metadata` with source filenames, reference, task, and required paths;
- one chart series per source report;
- grouped pgbench result tables;
- grouped PostgreSQL log references when available;
- non-required differences rendered as report/value comparison tables;
- deep-copied raw benchmark evidence in `joined_benchmark_runs`.

## Expected failures

- required value differs: the runs are not comparable under the selected task;
- missing dotted path: the report is incomplete for that task;
- schema mismatch: migrate or regenerate the older report;
- duplicate `report_name`: rename or regenerate one source;
- incomplete result structure: use benchmark reports rather than collection
  reports.

## Automation

`--machine` writes one JSON envelope to stdout and logs to stderr. Use `plan`
to validate and hash a redacted configuration without touching a target:

```bash
pg-perf-bench --machine plan collect-sys-info --connection-type local
```

Stable exit codes and the complete machine contract are documented in the root
README.
