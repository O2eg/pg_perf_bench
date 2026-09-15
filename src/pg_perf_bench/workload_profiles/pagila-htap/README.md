# Pagila HTAP profile

The `pagila` OLTP mix with one addition: `05_reporting.sql`, selected for about 5 % of
transactions, runs the reporting views and 30-day aggregates (`sales_by_store`,
`sales_by_film_category`, `film_list`, category and daily revenue roll-ups,
`rewards_report`) over the whole dataset while the short transactions keep running. Compare
its curve with `pagila` to measure how much analytical load costs the shop: lock and buffer
contention, planner and CPU pressure, and the effect of `work_mem`, parallel query and
`shared_buffers` on mixed workloads.

Schema, generator, common-loader assets, setup script and the four OLTP scripts are byte-identical copies of the
`pagila` profile (a unit test enforces this); only the manifest and the reporting script
differ. Weights: `01_select` 50, `02_insert` 25, `03_update` 20, `04_delete` 5,
`05_reporting` 5. Everything said about scale, determinism, prepared mode and
`--random-seed=42` in the `pagila` README applies here.

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile pagila-htap --workload-scale 4 --pgbench-clients 1,2,4,8,16,32`

Use a dedicated disposable database; the benchmark resets the database or profile schemas before every point.
