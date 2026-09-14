# Pagila OLTP profile

Pure OLTP on the DVD-rental model: short transactions that look up, insert, update and
delete rows by primary key or indexed foreign key. Use it for connection scaling, WAL,
checkpoint, lock and storage-latency experiments and as the baseline for `pagila-htap`.

The deterministic generator creates countries, cities, customers, actors, films, inventory,
rentals and payments. `--workload-scale 1` creates roughly the traditional Pagila
cardinalities and about 9 MB of data; scale 4 holds about 28 MB (template database
excluded). The whole database therefore sits in `shared_buffers` at low scales; raise the
scale until the data exceeds the cache under test. The schema and generator run unchanged on
PostgreSQL 10–18, and pseudo-random values come from `hashint8()`, so the dataset is
identical on every server major.

Four pgbench scripts run with fixed weights: `01_select` 50 %, `02_insert` 25 %,
`03_update` 20 %, `04_delete` 5 %. Every random identifier is chosen by pgbench within
table bounds read from the one-row `bench_bounds` table, statements run in prepared mode,
and dates are offsets
inside the generated 2022 range, so the cost of a transaction does not depend on table size
and the script sequence is reproducible under `--random-seed=42`. Every insert transaction
records a rental with its payment; customer registration (10 %), new films (5 %), extra
inventory (10 %), new staff and new stores (0.2 % each) are gated by pgbench-side
probabilities so the data keeps a shop-like shape during the measured window.

`sql/pagila-benchmark-setup.sql` runs after the generator: it adds the Sakila indexes that
Pagila dropped (rental by customer, inventory by film, film_category by category, payment by
rental), sets `search_path` for the database and for the benchmark role inside it (the
schema-less Pagila functions need it, and a role-level `search_path` would otherwise win),
refreshes the
materialized view, fills `bench_bounds` and runs `VACUUM (FREEZE, ANALYZE)` so the measured window starts with
frozen pages and fresh statistics.

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile pagila --workload-scale 4 --pgbench-clients 1,2,4,8,16,32`

Use a dedicated disposable database. Every point is initialized from the same generator,
which prevents mutations from an earlier concurrency point contaminating a later one.
