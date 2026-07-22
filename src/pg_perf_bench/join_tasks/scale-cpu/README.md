# Measure CPU scaling

Use this scenario to answer: “If CPU capacity increases, how much will maximum TPS grow
for the same database configuration and workload?” CPU model, socket/core/thread count,
frequency limit or VM vCPU quota may vary. RAM, storage, OS settings, PostgreSQL effective
settings, dataset, query mix and pgbench sweep must remain identical.

## Operator workflow

1. Pin one PostgreSQL configuration and one workload evidence hash.
2. Benchmark each CPU size with the same client sweep. A sweep is essential: the old
   client count may no longer saturate the larger CPU.
3. Join reports with `--join-task scale-cpu`.
4. Calculate both absolute gain and scaling efficiency:
   `efficiency = TPS_new / TPS_old / (CPU_new / CPU_old)`.

Also inspect latency and storage utilization. A flat TPS curve usually means the system
became limited by storage, locks, memory bandwidth or the workload itself; it is not proof
that the additional CPUs are defective.
