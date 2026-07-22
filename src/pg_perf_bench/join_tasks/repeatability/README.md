# Check repeatability and performance regression

Use this scenario when nothing is intended to change. It estimates benchmark variance,
validates a suspicious result and provides a strict performance-regression comparison.
Every captured environment, effective DB setting, workload file and pgbench parameter is
controlled.

## Operator workflow

1. Run at least three repetitions using the same reset, cache and warm-up policy.
2. Put those reports in one directory and join with `--join-task repeatability`.
3. Compare the distribution at each client count, not only the single best TPS value.
4. Investigate background CPU, I/O, thermal throttling, autovacuum and noisy neighbours if
   spread is larger than the acceptance threshold for the system.

If the join rejects a report, the runs were not identical according to captured evidence.
That mismatch is itself useful: resolve it before treating a TPS difference as regression.
