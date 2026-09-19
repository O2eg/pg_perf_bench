# IMDb analytical profile

A deterministic synthetic movie dataset with 21 tables and 38 read-only pgbench
scripts: five grouped reports plus 33 multi-join families, **118 SELECT statements**
in total. The join families reference 4–17 tables and exercise join ordering,
selective lookups, aggregation and memory/cache behavior. This is a synthetic
analytical/planner workload, not a production IMDb export or a simulation of a
movie application's request distribution. Independent `MIN` values in the legacy
join families are aggregate probes; they do not describe one coherent movie row.

## Data and scale

Scale 1 generates 100,000 titles and 100,000 people, approximately 2.6 million rows
before relationship deduplication, plus a small fixed set of compatibility fixtures.
The report records actual sizes. IMDb scale 50 is much larger than Pagila scale 50;
compare actual rows/bytes rather than treating profile scale units as equivalent.

Every title has cast, keywords, companies, attributes, one rating and one vote count.
Attribute types distinguish numeric values (ratings, votes, ranks) from textual
metadata. Numeric attributes retain the original text storage but queries and the
numeric expression index use numeric comparison and aggregation. Budget minima
also use numeric USD amounts; output labels distinguish genres, ratings and votes. Top-250 and
bottom-10 lists are disjoint, globally ordered movie rankings, with rating, votes
and title ID providing deterministic tie-breaking. Keyword, attribute and link
relations are deduplicated; one cast-completeness status is kept per title/subject.
Company ratings average titles once per company, independently of the number of
company credits for that title.

Modular mappings choose a stride coprime to the actual title cardinality. This
includes fractional scales such as 0.08191, 0.32771 and 0.65537, which collided with
the former fixed multipliers. Countries and title kinds occur throughout the
scaled background data. Episodes reference an existing series and have valid
season/episode metadata; aliases copy the source title metadata. Link targets
are deterministic, distinct from their source, and sampled across the title range.
For this synthetic model, sequel/follows/features/references links point toward
an earlier title; the inverse link kinds point toward a later title.

Production years are frozen in 1950–2022; generated release years extend through
2024. Release dates and dated company notes cannot precede production. SQL uses
explicit historical windows, not the machine's current date. Old named compatibility
fixtures remain small and intentionally dense. Selective combinations can return
an all-NULL aggregate, especially at small scales; distinguish this from a useful
nonempty result when interpreting timings. Not every query's result cardinality
scales linearly: names and fixed fixture subgraphs are not replicated with scale.

## Approximate database sizes

Reference footprint on PostgreSQL 18.6 with the corrected generator and indexes,
before running the workload: scale 1 occupied **510,432,959 bytes** in the
database, including **215,810,048 bytes of table storage** and **284,008,448 bytes
of indexes**. The remaining approximately 10.6 MB is database overhead.

| Target database size, including indexes | Approximate `--workload-scale` | Table storage | Index storage | Titles |
| --- | ---: | ---: | ---: | ---: |
| 1 GB | 2 | 0.43 GB | 0.57 GB | 200,000 |
| 10 GB | 20 | 4.3 GB | 5.7 GB | 2,000,000 |
| 100 GB | 200 | 43 GB | 57 GB | 20,000,000 |
| 1 TB | 2000 | 432 GB | 568 GB | 200,000,000 |

Units are decimal: **1 GB = 10^9 bytes; 1 TB = 1000 GB**. Targets and scale
values are rounded starting points, obtained by extrapolating measured table and
index storage; fixed database overhead is not multiplied by scale. These are
estimates, not measured large-scale runs. Generated string lengths, index depth,
page occupancy and server settings can change the actual size. Check the report's
before-workload storage measurements after loading the desired scale.

The estimates exclude WAL, temporary files used by initialization or queries,
backups and additional replica copies. They describe one database on one node,
not the required free disk space. Scale units differ between profiles.

## Execution and interpretation

All random data uses `hashint8()` rather than `random()`. The common initializer
loads bounded batches, builds indexes after data loading, restores durability
settings and waits for replication before pgbench. Changing loader batch size or
worker count does not change logical data. `--init-mode legacy` installs the same
semantic keys and secondary indexes. See [initialization](../../../../INITIALIZATION.md).

Indexes support selective numeric attributes, company/title, keyword/title,
person/title and textual attribute lookups. Broad reports intentionally aggregate
large parts of the catalog; PostgreSQL can choose sequential scans for them.
Planner methods are not disabled. Increasing scale beyond cache changes the
experiment; a dataset that fits in memory primarily measures CPU, joins and memory.

The default protocol is simple; `--pgbench-prepared` selects prepared statements.
Each of the 38 scripts has the same selection weight, although they contain
different numbers of SELECTs and have different costs. A completed pgbench
transaction here means a script execution, not one SELECT or one business event.
`--random-seed=42` repeats choices with the same client configuration; timed runs
can complete different mixes. The default measured duration is 120 seconds and
can be changed with `--workload-duration-seconds`.

Example (connection and reset-safety arguments omitted):

`pg-perf-bench benchmark --workload-profile imdb --workload-scale 1 --pgbench-clients 1,2,4,8,16`

Use a dedicated disposable database. Every concurrency point reinitializes its
profile schemas. These generator and numeric-query corrections change workload
semantics and hashes: regenerate data and rerun both environments for comparisons.
