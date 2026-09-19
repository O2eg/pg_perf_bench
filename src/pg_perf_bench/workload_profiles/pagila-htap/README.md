# Pagila HTAP profile

The `pagila` OLTP mix plus a store dashboard (`05_reporting.sql`, weight 5 out of
105). Each reporting transaction selects one store, one category and a period
of at most 30 days inside the generated rental timeline. All period boundaries
and daily groups use UTC, independently of the server's `TimeZone`.

The seven dashboard queries return cashier revenue, category revenue, catalog
counts for the selected category, daily rentals of that category, its most-rented
titles, daily cash receipts, and loyalty candidates. Revenue is attributed to
payment dates; `paid_rentals` counts distinct rentals within a payment day.
Rentals are counted directly from `rental`, so extra payments do not inflate them.
Store attribution follows the issuing cashier's store, not a copy's current
location (inventory can move). The workload does not transfer an issuing cashier
to another store; a new store can assign a previously non-manager staff member.

The profile uses selective store/cashier, period and category predicates and
composite indexes. It does not run global all-history views, mix stale materialized
totals with live counters, or call the legacy wall-clock `rewards_report` function.
Small dimensions may still use sequential scans when PostgreSQL considers them
cheaper. No planner methods are disabled.

Schema, generator, both initialization paths, indexes and the four OLTP scripts
are byte-identical to `pagila`. Weights remain `01_select` 50, `02_insert` 25,
`03_update` 20, `04_delete` 5, `05_reporting` 5. Reporting probability is 5/105
(about 4.76%); its fraction of elapsed client time depends on query latency.
Clients share this mix, so the profile does not reserve a separate analyst pool.
The seven statements run in a read-only transaction at the connection's isolation
level; default READ COMMITTED does not promise a single cross-statement snapshot.

Returns look up the open rental by inventory ID using the partial unique index,
including rentals created during the run. A returned copy can be rented again;
returns are not restricted to the initial rental ID range. Inventory moves
lock the copy first and check availability in a separate READ COMMITTED
statement, so a rental committed during the lock wait prevents the move.

The revised workload changes both data and business operations. Regenerate data
through the normal initializer and rerun both profiles when making comparisons;
old global-reporting results are not measurements of the same workload.

## Approximate database sizes

Reference footprint on PostgreSQL 18.6 with the corrected generator and indexes,
before running the workload: scale 50 occupied **517,161,487 bytes** in
the database, including **190,332,928 bytes of table storage** and **314,695,680
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

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile pagila-htap --workload-scale 30 --pgbench-clients 16,64,128`

Use a dedicated disposable database; initialization resets the database or profile
schemas before each point. See the [Pagila profile](../pagila/README.md) for the
shared data model and initialization details.
