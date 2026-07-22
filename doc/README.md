# pg_perf_bench documentation

The root [README](../README.md) describes the utility, its safety contract, and
the main CLI workflows. This directory contains focused operational guides.

## Guides

| Document | Use it when |
|---|---|
| [Local transport](local_mode_usage.md) | PostgreSQL and the workload generator run on the same host |
| [Docker transport](docker_mode_usage.md) | PostgreSQL runs in an existing container |
| [SSH transport](ssh_mode_usage.md) | PostgreSQL runs on a remote host reached through SSH |
| [Workload configuration](workload_description.md) | You are defining the init command, measured command, and iteration axis |
| [Reports and comparisons](logic_building_and_comparing_reports.md) | You are consuming JSON/HTML artifacts or joining benchmark runs |
| [`pg_play` integration](pg_play-integration.md) | You are invoking the versioned machine contract from an orchestrator |

## Reading order

For a new benchmark:

1. Read the safety contract in the root README.
2. Select and configure one transport.
3. Define a workload and verify all placeholders with `plan`.
4. Run against a disposable database.
5. Validate and compare the resulting artifacts.

## Conventions

- Examples use PostgreSQL 18 paths; adjust them for the target installation.
- `PGPASSWORD=secret` is illustrative. Use an appropriate secret-injection
  mechanism and never commit real credentials.
- `pg_perf_bench_test` always means a dedicated disposable database.
- The workload-generator host is the machine running `pg-perf-bench`,
  `pgbench`, and `psql`.
- The target host is the machine or container whose PostgreSQL instance is
  controlled and inspected.

All documentation is text-only. Generated benchmark HTML may still contain
interactive charts rendered by the embedded ECharts library.
