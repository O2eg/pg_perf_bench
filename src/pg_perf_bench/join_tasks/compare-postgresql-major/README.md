# Compare PostgreSQL major versions

Use this scenario to quantify the throughput and latency impact of a PostgreSQL major
upgrade. PostgreSQL version, build metadata and effective settings may differ because
defaults and valid parameters change between majors. Hardware, OS and workload evidence
must be identical.

## Operator workflow

1. Initialize each major from the same workload generator and scale; do not compare one
   fresh database with another production-aged database.
2. First compare with version defaults, or separately provide equivalent hand-tuned
   configurations. Do not mix those two experiments.
3. Use the same client sweep and join with `--join-task compare-postgresql-major`.
4. Inspect version, extension, build and `pg_settings` differences in the joined report,
   then compare peak TPS and latency.

An extension-version change can affect results even when PostgreSQL itself is the intended
variable. Call it out explicitly or pin compatible extension versions when possible.
