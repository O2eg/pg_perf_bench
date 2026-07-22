# JOIN scenario catalog

JOIN scenarios are comparison contracts, not arbitrary lists of report fields. Each
scenario declares which dimensions must be identical before TPS series may be put on
one chart and which dimension is intentionally allowed to change.

Run `pg-perf-bench join-tasks` to list the catalog. Use
`pg-perf-bench join --join-task SCENARIO_ID ...`. Read the scenario README before
collecting reports: it explains what must be fixed, what may vary, and how to interpret
the result. `task.json` is consumed by the CLI; `README.md` is the operator runbook.

Available scenarios:

- `optimize-db-config`: find the best PostgreSQL configuration on one fixed system.
- `scale-cpu`: estimate TPS gain from adding CPU while keeping DB settings and load fixed.
- `scale-memory`: estimate the benefit of adding RAM.
- `compare-storage`: compare storage devices or storage layouts.
- `tune-os-kernel`: measure an OS/kernel tuning change on identical hardware.
- `compare-postgresql-major`: quantify a PostgreSQL major-version change.
- `repeatability`: detect benchmark noise and regressions between identical reruns.

The join stops on the first controlled-dimension mismatch. Do not remove a comparison
item just to force incompatible reports to join; choose another scenario or rerun the
benchmark with controlled inputs.

New reports expose stable dimension hashes for CPU, memory capacity, storage hardware,
network hardware, kernel/OS, and the local load generator. Tasks compare those hashes
instead of volatile `/proc/meminfo`, filesystem free space, or report-specific CLI
arguments.
