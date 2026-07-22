# Workload profile collection

These profiles are benchmark inputs for `pg_perf_bench`. They contain the complete data
schema, deterministic Python data generator and pgbench query files needed to reproduce a
maximum-TPS measurement. They intentionally do not contain `profile.yml`: that manifest
belongs to the scheduling semantics of `pg_workload`, while `pg_perf_bench` controls a
client sweep, database reset and evidence collection itself.

List installed profiles with `pg-perf-bench profiles`. Select one with
`pg-perf-bench benchmark --workload-profile imdb ...` or `pagila`. The profile supplies
the custom initialization/workload command templates; `--workload-scale` controls data
volume and `--pgbench-clients` controls the concurrency sweep. Both values and every
resolved command are embedded in the JSON report together with the full SQL/Python source.

The copied profile assets originated in `pg_workload/bundled/profiles`; see the project
`THIRD_PARTY_NOTICES.md` for provenance.
