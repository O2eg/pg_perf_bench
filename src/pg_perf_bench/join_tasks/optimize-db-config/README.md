# Optimize PostgreSQL configuration

Use this scenario when the question is: “Which PostgreSQL configuration gives the
highest sustainable TPS for this workload on this server?” Hardware, OS tuning,
PostgreSQL build, dataset, query mix and every pgbench parameter are controlled. The
effective `pg_settings` snapshot is deliberately allowed to differ.

## Operator workflow

1. Choose one bundled or custom workload and one dataset scale. Keep the same report
   command, client sweep, duration, warm-up policy and cache policy for every run.
2. Change only the PostgreSQL configuration. Restart PostgreSQL if the setting requires
   it, then create a new benchmark report with a unique name.
3. Put only these reports in one input directory and run:

   `pg-perf-bench join --join-task optimize-db-config --input-dir reports --reference-report baseline.json`

4. Compare peak TPS, the client count at peak TPS, latency and the embedded effective
   settings. Prefer a configuration that improves repeated results, not a single spike.

If the join rejects a report, an environment or workload control changed. Restore that
control and rerun; do not treat the rejected result as evidence for a configuration win.
