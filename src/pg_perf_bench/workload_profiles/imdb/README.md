# IMDb analytical profile

This profile stresses multi-table joins, aggregation and selective lookups on a synthetic
movie-domain schema. Use it for analytical PostgreSQL configuration, CPU-scaling and
memory/cache experiments.

The deterministic generator creates companies, people, titles, cast, keywords and movie
metadata. `--workload-scale 1` creates about 100,000 titles, 100,000 people and more than
one million relationship rows; scale all major cardinalities together for larger systems.
Five pgbench scripts are selected with equal probability and run for 60 seconds per client
count.

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile imdb --workload-scale 1 --pgbench-clients 1,2,4,8,16 --pgbench-path pgbench --psql-path psql`

Use a dedicated disposable database. The benchmark recreates it before every point in the
client sweep, so every point receives the same generated data and indexes.
