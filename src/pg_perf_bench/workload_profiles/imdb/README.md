# IMDb analytical profile

This profile is the complete analytical workload shared with `pg_workload/imdb`: 38 pgbench
scripts with 113 read-only SELECT variants over a synthetic 21-table movie-domain schema.
Five compact scripts (`01_*`–`05_*`) cover grouped analytics; `select_1.sql` through
`select_33.sql` are multi-join families that reference 4–17 tables each (median 8) and
exercise join ordering, hash/merge/nested-loop choices, selective filters and correlated
subgraphs. Use it for analytical PostgreSQL configuration, planner-setting, CPU-scaling and
memory/cache experiments.

The deterministic generator creates companies, people, characters, titles, cast, keywords,
movie attributes, indexed attributes and movie links. `--workload-scale 1` creates 100,000
titles, 100,000 people and about 2 million fact, relationship and attribute rows; scale all
major cardinalities together for larger systems. Measured on PostgreSQL 18, scale 1 holds
about 330 MB of table and index data (template database excluded) and initializes in about
10 seconds. Pick a scale whose data exceeds the cache you want to test, otherwise the sweep
measures CPU on a fully cached working set. All pseudo-random columns come from `hashint8()`,
not `random()`, so the dataset is byte-identical on PostgreSQL 10–18.

Script cost ranges from a few milliseconds to about two seconds per transaction at scale 1,
so the measured window defaults to 120 seconds (`--workload-duration-seconds` overrides it)
and pgbench runs with `--random-seed=42`: every iteration and every compared report executes
the same script sequence, which removes mix-composition noise from A/B comparisons. Without
the fixed seed two identical 30-second runs differed by about 7 %; with it they agree within
1 %.

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile imdb --workload-scale 1 --pgbench-clients 1,2,4,8,16`

Use a dedicated disposable database. The benchmark recreates it before every point in the
client sweep, so every point receives the same generated data and indexes.
