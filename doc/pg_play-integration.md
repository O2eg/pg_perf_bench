# pg_play integration contract

This document is for orchestrator authors. Normal users retain the benchmark,
collection, render, and join CLI documented in the main README.

`pg_perf_bench` supports the hidden `pg_play/component/v1` machine transport:

```bash
pg-perf-bench --machine --request-id bench-001 --component-capabilities
pg-perf-bench --machine --request-id bench-002 validate
pg-perf-bench --machine --request-id bench-003 summarize report.json
pg-perf-bench --machine --request-id bench-004 validate-artifact report.json
```

The capability document uses `pg_play/capabilities/v1`. Every command declares
the common boolean fields `mutates_target`, `machine_output`, and
`accepts_plan_hash`.
Its `machine_interface` object records the canonical machine, request-id, and
capability option names.

`benchmark` resets its selected disposable database or profile schemas and is a
mutating operation. Machine execution requires a plan hash from the exact same
arguments and workload content:

```bash
pg-perf-bench --machine --request-id bench-plan \
  plan benchmark ...
pg-perf-bench --machine --request-id bench-run \
  benchmark ... --plan-hash sha256:...
```

Changing an argument, custom workload file, or workload directory member makes
the reviewed hash stale. Output, log, request-id, and report-name locations do
not change the execution hash.

JSON and HTML outputs are returned as common artifact descriptors containing
`kind`, `schema_version`, absolute `path`, SHA-256 `hash`, and `size_bytes`.
`summarize` validates `pg_perf_bench/report-v1` first and returns deterministic
iteration, collection-status, and TPS evidence. Passwords remain outside the
machine document and are redacted from errors, plans, logs, and reports.

The benchmark capability advertises `managed_option=--managed`, optional
`managed_pg_info_option=--managed-pg-info` and `reset_modes=[database, schema]`.
The reset policy is part of the runtime plan and workload execution hash.
