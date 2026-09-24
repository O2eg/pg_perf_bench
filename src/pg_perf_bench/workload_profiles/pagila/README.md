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

Reference PostgreSQL 18 footprint for the current model at scale 72 is approximately
**805 MB per database**, including **296 MB of table storage** and **499 MB of indexes**.
Pagila and Pagila HTAP share the same initial data and indexes; subsequent writes
change their sizes. Figures are rounded, and fixed database overhead is not scaled.

| Target database size, including indexes | Approximate `--workload-scale` | Table storage | Index storage | Films |
| --- | ---: | ---: | ---: | ---: |
| 1 GB | 90 | 0.37 GB | 0.62 GB | 90,000 |
| 10 GB | 900 | 3.7 GB | 6.2 GB | 900,000 |
| 100 GB | 9000 | 37 GB | 62 GB | 9,000,000 |
| 1 TB | 90000 | 370 GB | 624 GB | 90,000,000 |

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
table bounds read from the one-row `bench_bounds` table. Historical read dates cover
the loaded rental, return and payment timeline, including small scales. Identifier selection avoids scans
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
customer/unpaid-fee lookup, film/store inventory, and open rentals. The film
card aggregates cast and inventory independently instead of multiplying them.
A fee operation locks one eligible rental and inserts its payment once. Existing
receipts are not repeatedly increased. Stock checks use EXISTS and the partial index
for open rentals.

## Data and time contract

Fractional scales are supported. Modular mappings use strides coprime to the actual
cardinality: stores/cities stay covered, and a film's three to seven actors are distinct.
Each generated rental/payment names the issuing store's manager. Copies receive
non-overlapping histories: independently distributed starting phases and rentals nine
days apart, lasting one to eight days. Only the last rental of a copy may remain open;
a partial unique index also prevents concurrent open rentals during the workload.

Each rental stores its agreed `rental_rate` and `rental_duration`. Catalog repricing
cannot change historical balances or due dates. Initial receipts total the agreed
base fee; rentals with an extra receipt split that fee into two installments instead
of generating an unrelated overpayment. Overdue charges are one currency unit per
complete day beyond the agreed duration, calculated separately for each rental.
`get_customer_balance(customer, effective_date)` includes only rentals, accrued fees
and receipts up to that date. Historical overdue queries include later returns.

`benchmark_now()` advances a logical clock from the day after the loaded history.
New rentals, immediate base-fee payments, registrations and returns use that clock.
A return closes the loan at the current logical time, so short benchmark rentals may
last only milliseconds; no randomly backdated rental or future-dated return is made.
After a late return, one eligible fee can be collected once via an atomic row-locked
operation. The historical accrued debt already exists; collecting it adds a new receipt.
Deletion locks the rental before removing payments, using the same lock order as fees.

Payment partitions cover January–July 2022, plus an indexed future range partition
for longer-running logical clocks. Payment amounts use `numeric(12,2)`. Film rates
stay within 0.99–5.99, durations within two to eight days, and replacement costs within
9.99–34.99 across generation and catalog updates.

Both initializer paths install the same indexes and compute `data_start_epoch`,
`data_days`, `report_window_days` and logical-clock anchors in `bench_bounds`. Time-window changes and
new data invariants require a fresh initialization; comparisons with older workload
versions must use new runs on both environments.
