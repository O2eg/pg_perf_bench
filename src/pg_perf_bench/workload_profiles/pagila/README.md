# Pagila mixed OLTP profile

This profile measures a mixed read/write transaction workload on a DVD-rental model. It
is useful for WAL, checkpoint, lock, storage and general OLTP configuration experiments.

The deterministic generator creates countries, cities, customers, actors, films,
inventory, rentals and payments. `--workload-scale 1` creates roughly the traditional
Pagila cardinalities; larger scales increase the related tables together. Four pgbench
scripts exercise selects, inserts, updates and deletes with equal probability for 60
seconds at each client count.

Example selection (connection and safety arguments omitted):

`pg-perf-bench benchmark --workload-profile pagila --workload-scale 1 --pgbench-clients 1,2,4,8,16 --pgbench-path pgbench --psql-path psql`

Use a dedicated disposable database. Every point is initialized from the same generator,
which prevents mutations from an earlier concurrency point contaminating a later one.
