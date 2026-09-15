# Pagila OLTP profile

Pure OLTP on the DVD-rental model: short transactions that look up, insert, update and
delete rows by primary key or indexed foreign key. Use it for connection scaling, WAL,
checkpoint, lock and storage-latency experiments and as the baseline for `pagila-htap`.

The deterministic generator creates countries, cities, customers, actors, films, inventory,
rentals and payments. `--workload-scale 1` creates roughly the traditional Pagila
cardinalities. Actual sizes depend on the server and index storage; the report records
them before and after the workload. Small scales fit in `shared_buffers`; raise the
scale until the data exceeds the cache under test. The schema and generator run unchanged on
PostgreSQL 10–18, and pseudo-random values come from `hashint8()`, so the dataset is
identical on every server major.

Four pgbench scripts run with fixed weights: `01_select` 50 %, `02_insert` 25 %,
`03_update` 20 %, `04_delete` 5 %. Every random identifier is chosen by pgbench within
table bounds read from the one-row `bench_bounds` table, statements run in prepared mode,
and dates are offsets inside the generated 2022 range. Identifier selection avoids scans
over growing tables, while transaction cost still depends on data size and caching.
`--random-seed=42` repeats random choices with the same client configuration; timed runs
can still complete different numbers of transactions and yield different mix proportions.
Every insert transaction
records a rental with its payment; customer registration (10 %), new films (5 %), extra
inventory (10 %), new staff and new stores (0.2 % each) are gated by pgbench-side
probabilities so the data keeps a shop-like shape during the measured window.

The [common initializer](../../../../INITIALIZATION.md) loads bounded batches into
UNLOGGED tables, converts them to LOGGED, and builds all indexes afterward with the
shared parallel scheduler. It supplies connection-local `search_path`, refreshes the materialized
view, fills `bench_bounds` and runs `VACUUM (FREEZE, ANALYZE)`. Original durability settings
are restored and directly connected replicas catch up before pgbench. Identifiers use
bigint, and generation does not depend on batch size or worker scheduling.

`sql/pagila-schema.sql` and `sql/pagila-benchmark-setup.sql` remain the legacy path
for `--init-mode legacy`; the generator CLI also uses bounded data batches.

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile pagila --workload-scale 4 --pgbench-clients 1,2,4,8,16,32`

Use a dedicated disposable database. Every point is initialized from the same generator,
which prevents mutations from an earlier concurrency point contaminating a later one.
