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
major cardinalities together for larger systems. The report records actual table and index
sizes before and after each workload. Pick a scale whose data exceeds the cache you want to
test, otherwise the sweep
measures CPU on a fully cached working set. All pseudo-random columns come from `hashint8()`,
not `random()`, so generated values are identical on PostgreSQL 10–18. Physical files and
row ordering can differ. The [common initializer](../../../../INITIALIZATION.md) loads in
bounded committed batches, builds indexes afterward in parallel and restores the normal
durability/replication behavior before pgbench. Batch size and worker count do not change
the generated values. `--init-mode legacy` preserves the command-based initialization path.

Script cost ranges from a few milliseconds to about two seconds per transaction at scale 1,
so the measured window defaults to 120 seconds (`--workload-duration-seconds` overrides it)
and pgbench runs with `--random-seed=42`. The seed makes random choices repeatable with
the same client configuration. Timed runs can complete different numbers of scripts;
the seed does not guarantee identical observed mix proportions or TPS.

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile imdb --workload-scale 1 --pgbench-clients 1,2,4,8,16`

Use a dedicated disposable database. The benchmark resets the database or profile schemas before every point in the
client sweep, so every point receives the same generated data and indexes.
