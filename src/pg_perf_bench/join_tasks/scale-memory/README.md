# Measure memory scaling

Use this scenario to estimate whether more RAM improves maximum TPS or latency without
changing PostgreSQL settings. This isolates the benefit of a larger OS page cache. It is
different from retuning `shared_buffers` or `work_mem`; use `optimize-db-config` for that.

## Operator workflow

1. Keep CPU, storage, PostgreSQL settings, dataset and pgbench sweep identical.
2. Change only available physical memory or the VM/container memory limit.
3. Reboot or otherwise start every run from the same cache state, then join with
   `--join-task scale-memory`.
4. Compare peak TPS and latency. Record whether the active dataset fits in memory at each
   size and repeat runs to separate cache effects from noise.

If changing the memory limit also changes PostgreSQL auto-tuning, the effective-settings
hash will differ and the join will stop. Either pin the configuration or treat that as a
separate configuration experiment.
