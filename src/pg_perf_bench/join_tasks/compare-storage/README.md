# Compare storage

Use this scenario to compare local SSDs, network volumes, RAID layouts or filesystems.
Storage inventory, mounts and capacity are allowed to differ. Compute, OS tuning,
PostgreSQL settings and workload must remain fixed.

## Operator workflow

1. Put PostgreSQL data, WAL and temporary files on the intended layout and document any
   separation in the report name.
2. Use the same initialized dataset and test policy. Ensure no other workload shares the
   device during a run.
3. Benchmark a client sweep and join with `--join-task compare-storage`.
4. Compare peak TPS and latency, then inspect the storage evidence in each source report
   before attributing the change to a device.

Filesystem mount options and I/O scheduler changes are part of the storage variant here.
If the goal is to isolate only an OS tuning switch on the same device, use
`tune-os-kernel` instead.
