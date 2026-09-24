# Pagila HTAP profile

The `pagila` OLTP mix plus a store dashboard (`05_reporting.sql`, weight 5 out of
105). Each reporting transaction selects one store, one category and a period
of at most 30 days covering the generated rental/return/payment timeline and the
current logical workload day. All generated receipt dates are reachable; the upper
window bound advances with `benchmark_now()` without rewriting a shared counter. All period boundaries
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

The shared [data and time contract](../pagila/README.md#data-and-time-contract)
defines non-overlapping copy histories, snapshotted rental terms, historical balances
and one-time collection of late fees. Both profiles require regenerated data after
this schema change.

Returns look up the open rental by inventory ID using the partial unique index,
including rentals created during the run. A returned copy can be rented again;
returns are not restricted to the initial rental ID range. Inventory moves
lock the copy first and check availability in a separate READ COMMITTED
statement, so a rental committed during the lock wait prevents the move.

The revised workload changes both data and business operations. Regenerate data
through the normal initializer and rerun both profiles when making comparisons;
old global-reporting results are not measurements of the same workload.

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

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile pagila-htap --workload-scale 30 --pgbench-clients 16,64,128`

Use a dedicated disposable database; initialization resets the database or profile
schemas before each point. See the [Pagila profile](../pagila/README.md) for the
shared data model and initialization details.
