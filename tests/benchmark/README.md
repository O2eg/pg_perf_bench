# Manual initialization speed test

`initialization_speed.py` measures the common loader on a disposable PostgreSQL
primary with **two synchronous physical replicas**, using replication slots.
Run it explicitly from the repository root; normal `pytest` discovery does not
run this benchmark. It needs the project installed (for example,
`python -m pip install -e '.[dev]'`) and a local Linux Docker daemon.

## Run

Load approximately **1 GiB of tables plus indexes** for Pagila, then IMDb:

```bash
python -m tests.benchmark.initialization_speed \
  --init-synchronous-commit off --output /tmp/pg-perf-load-1g
```

The output directory must be new. Omit `--output` to create a unique temporary
directory automatically. For a quick check at approximately 10 MiB per profile:

```bash
python -m tests.benchmark.initialization_speed --target-gib 0.01
```

Select profiles, size and loader concurrency:

```bash
python -m tests.benchmark.initialization_speed \
  --profiles imdb --target-gib 2 --workers 4 --batch-rows 100000
```

Available profiles: `pagila`, `pagila-htap`, `imdb`. The default image is
`postgres:18`; `--image` can select a compatible official PostgreSQL image or pin
its digest. `--timeout` bounds each loader operation (default: 1800 seconds).
The benchmark creates its own containers, network and data volumes, and removes
them on completion or failure. Previously running containers are not selected.
Disk space is checked on the Docker data filesystem; allow room for three copies
of the dataset, table rewrites, indexes and WAL (the check requires at least seven
times the target size, with a 1 GiB minimum, before each load).

## What is measured

Each profile gets a calibration load in a fresh database. Its measured size
determines the scale for the target load. Up to three target attempts are allowed
to reach **98–107%** of the requested size; every attempt is retained in the JSON
results. A target smaller than the profile's minimum dataset may be unattainable;
the command then fails with the results path.

The selected target measurement includes:

1. Schema setup and conversion of stored tables to UNLOGGED.
2. Batched data generation and insertion.
3. Conversion to LOGGED, parallel index builds and constraint validation.
4. Profile finalization, VACUUM and ANALYZE.
5. Restoring fsync, CHECKPOINT and host `sync`.
6. Waiting until both replicas have replayed the preparation WAL.

Defaults match the accelerated loader: four workers, 100000 rows per batch,
temporary `fsync=off` on the primary and unchanged commit policy (`keep`) in loader
connections. Pass `--init-synchronous-commit off` (as in the accelerated example)
to disable acknowledgement/flush waits during loading, or `local` to retain local
flush waits. Ordinary sessions use `remote_apply` with two synchronous replicas.
The script verifies restoration of the settings, valid indexes/constraints,
LOGGED tables and no unapplied preparation WAL on either replica.

Container and database creation, calibration, subsequent validation queries and
pgbench are outside the measured interval. This is **one selected measurement per
profile after calibration**; OS caches are not cleared. All three nodes share the
host's CPU, memory and storage. Container shared memory is 512 MiB per node;
PostgreSQL settings otherwise use the image defaults. Timings are observations,
not CI pass/fail thresholds.

## Results

- `summary.md`, `summary.csv`: selected results for comparison.
- `results.json`: every calibration and target attempt; `selected: true` marks
  the attempt included in the summary.
- `<profile>-<attempt>.json`: detailed phases, sizes, row/batch counts, WAL volume,
  replication state and settings restoration for that attempt.
- `environment.json`: PostgreSQL and loader settings, image ID, CPU/RAM,
  free disk space, Git HEAD, dirty checkout flag and invocation options.
- `progress.log`: per-task progress and errors.
- `state/`: fsync recovery journals while the disposable primary is being prepared.
