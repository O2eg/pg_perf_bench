# Pagila OLTP profile

Pure OLTP on the DVD-rental model: short transactions that look up, insert, update and
delete rows by primary key or indexed foreign key. Use it for connection scaling, WAL,
checkpoint, lock and storage-latency experiments and as the baseline for `pagila-htap`.

The deterministic generator creates cities, customers, actors, films, inventory,
rentals and payments; the country dimension stays at 109 entries as scale grows. `--workload-scale 1` creates roughly the traditional Pagila
cardinalities. Actual sizes depend on the server and index storage; the report records
them before and after the workload. Small scales fit in `shared_buffers`; raise the
scale until the data exceeds the cache under test. The schema and generator run unchanged on
PostgreSQL 10–18, and pseudo-random values come from `hashint8()`, so the dataset is
identical on every server major.

## Approximate database sizes

Reference footprint on PostgreSQL 18.6 with the corrected generator and indexes,
before running the workload: scale 50 occupied **517,469,887 bytes** in
the database, including **190,332,928 bytes of table storage** and **314,703,872
bytes of indexes**. The remaining approximately 12 MB is database overhead.
Pagila and Pagila HTAP use the same initial data and indexes, so their initial
size estimates are the same; the subsequent write workload can change their sizes.

| Target database size, including indexes | Approximate `--workload-scale` | Table storage | Index storage | Films |
| --- | ---: | ---: | ---: | ---: |
| 1 GB | 100 | 0.38 GB | 0.63 GB | 100,000 |
| 10 GB | 1000 | 3.8 GB | 6.3 GB | 1,000,000 |
| 100 GB | 10000 | 38 GB | 63 GB | 10,000,000 |
| 1 TB | 100000 | 381 GB | 629 GB | 100,000,000 |

Units are decimal: **1 GB = 10^9 bytes; 1 TB = 1000 GB**. Targets and scale
values are rounded starting points, obtained by extrapolating measured table and
index storage; fixed database overhead is not multiplied by scale. These are
estimates, not measured large-scale runs. Generated string lengths, index depth,
page occupancy and server settings can change the actual size. Check the report's
before-workload storage measurements after loading the desired scale.

The estimates exclude WAL, temporary files used by initialization or queries,
backups and additional replica copies. They describe one database on one node,
not the required free disk space. Scale units differ between profiles.

Four pgbench scripts run with fixed weights: `01_select` 50 %, `02_insert` 25 %,
`03_update` 20 %, `04_delete` 5 %. Every random identifier is chosen by pgbench within
table bounds read from the one-row `bench_bounds` table, and dates are offsets
inside the actual loaded 2022 rental range, including small scales. Identifier selection avoids scans
over growing tables, while transaction cost still depends on data size and caching.
`--random-seed=42` repeats random choices with the same client configuration; timed runs
can still complete different numbers of transactions and yield different mix proportions.
Each insert script attempts to rent an available copy with its payment; unavailable
or concurrently locked copies are skipped. Customer registration (10 %), new films (5 %), extra
inventory (10 %), new staff and new stores (0.2 % each) are gated by pgbench-side
probabilities so the data keeps a shop-like shape during the measured window.

Returns look up the open rental by inventory ID using the partial unique index,
including rentals created during the run. A returned copy can be rented again;
returns are not restricted to the initial rental ID range. Inventory moves
lock the copy first and check availability in a separate READ COMMITTED
statement, so a rental committed during the lock wait prevents the move.

The default query protocol is simple. Add `--pgbench-prepared` for prepared
statements, and use the same setting on both sides of a comparison.

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

Selective access paths cover customer/date history, cashier/date activity,
customer/latest-payment lookup, film/store inventory, and open rentals. The film
card aggregates cast and inventory independently instead of multiplying them.
Only one payment is changed by the latest-payment operation. Stock checks use
EXISTS and the partial index for open rentals.

Each generated rental/payment names the issuing store's manager. Only the latest
rental of a copy may be initially open; a partial unique index prevents duplicate
open rentals during concurrent inserts. Rental dates remain synthetic historical
events, and the generator does not model every physical overlap or customer journey.
The effective-date balance calculation excludes fees after the requested date.

Both initializer paths install the same indexes and compute `data_start_epoch`,
`data_days` and `report_window_days` in `bench_bounds`. Time-window changes and
new data invariants require a fresh initialization; comparisons with older workload
versions must use new runs on both environments.
