# Compare PostgreSQL deployments

Use `compare-deployments` for an end-to-end comparison of managed PostgreSQL and
a self-managed deployment, including a Patroni primary with physical replicas.
Follow the [paired-run example](../../../../doc/managed_mode_usage.md#compare-managed-postgresql-and-patroni).

## Controlled inputs

Use the same utility version, profile sources, scale, client counts, duration,
local PostgreSQL clients, initializer options, reset policy and cache policy.
Run both commands from the same load-generator host. The task checks the workload
execution hash, client-tool identity, reset mode, restart policy and cache policy.
It does not prove that endpoints have equal network latency or hardware.

## Intentional differences

Deployment, hardware, PostgreSQL configuration, replication and pooling may differ.
Record those differences in each source report. To compare platforms under equivalent
durability requirements, match synchronous acknowledgement policy and replica count
before collecting reports. A different replica count or synchronous policy is a
separate comparison: its performance effect is included in the result.

## Workflow

1. Pre-create dedicated benchmark databases and grant the required monitoring rights.
2. Use SQL-only access (`--managed`) and schema reset for both sides, with metadata
   supplied for both reports or omitted from both.
3. Keep every intended physical replica connected before starting each command.
4. Run the paired commands; initialization must finish its replay barrier on each side.
5. Join only those source JSON files, using unique report names:

```bash
pg-perf-bench join --input-dir report/paired --reference-report managed-pagila.json \
  --join-task compare-deployments --out report/comparisons --report-name managed-vs-patroni
```

Inspect errors and missing diagnostics before interpreting the comparison. A passing
JOIN validates the declared controlled inputs; it does not certify equal deployments.
