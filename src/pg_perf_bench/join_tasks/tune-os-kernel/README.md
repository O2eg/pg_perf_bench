# Measure OS and kernel tuning

Use this scenario for changes such as huge pages, CPU governor, I/O scheduler, sysctl or
mount options on the same machine. Kernel, sysctl and mount evidence may differ; hardware,
PostgreSQL effective settings and workload may not.

## Operator workflow

1. Change one coherent OS policy at a time and reboot when required.
2. Verify PostgreSQL did not auto-adjust its settings after the reboot.
3. Use the same dataset, cache policy and pgbench client sweep, then join with
   `--join-task tune-os-kernel`.
4. Read the differing OS sections alongside TPS and latency. Repeat baseline/tuned runs
   in alternating order to reduce bias from background load or thermal state.

If hardware capacity, PostgreSQL settings or workload also changed, split the experiment;
otherwise the result cannot identify which change caused the performance difference.
