# Synchronizing source to a load-generator host

Keep one authoritative Git checkout, make and review changes there, then synchronize
it to the load-generator host. SQL connects directly to PostgreSQL or its session
pool through `--host` and `--port`; SSH is used for host operations and OS metrics.

The repository helper `scripts/sync_sidecar.py` mirrors the checkout to
`~/pg_perf_bench` on a remote load-generator host. Both the SSH destination and
private-key path are required arguments:

```bash
python3 scripts/sync_sidecar.py --host bench@load-generator.example --key /path/to/key --dry-run
python3 scripts/sync_sidecar.py --host bench@load-generator.example --key /path/to/key
```

The helper expects an existing `~/pgpb-venv` Python 3.12 environment with the
project dependencies and CLI installed. Dependency changes require a separate
environment update; synchronization does not install dependencies.

Do not synchronize while a benchmark is active. The helper checks for an active
CLI benchmark before changing files. A dry run only previews the rsync changes.
Before a real synchronization it backs up the remote source and source-path
configuration under `~/bench/pgpb_source_sync_<UTC timestamp>/`.

The mirror includes removal of files that no longer exist in the checkout.
Git data, virtual environments, caches, local `.env` files and build/runtime
outputs are excluded. Benchmark results under `~/bench` are outside the mirror.

The existing `~/pgpb-venv/bin/pg-perf-bench` command imports directly from
`~/pg_perf_bench/src` through the environment's `pg_perf_bench_source.pth` file.
SQL, templates and other resources are read from that source tree. No wheel build
or package reinstall is required. The helper checks the resulting import path
and runs `pg-perf-bench validate`; start a fresh process to use the updated code.

To roll back, restore the saved `source/` directory and `.pth` configuration
identified by the selected backup's `state.json`. If `pth_existed` is false,
remove only `pg_perf_bench_source.pth` to return to the installed package.
