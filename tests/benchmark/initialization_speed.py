"""Manual loader speed test on a disposable primary and two synchronous replicas.

Run from the checkout: python -m tests.benchmark.initialization_speed --help
Importing this module does not provision a stand or execute a benchmark.
"""

import argparse
import asyncio
import csv
import json
import logging
import math
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import asyncpg
import docker

from pg_perf_bench.connections.docker import DockerConnection
from pg_perf_bench.const import WORKLOAD_PROFILES_PATH, ConnectionType
from pg_perf_bench.initialization import LoadOptions, load_plan
from pg_perf_bench.initialization_settings import initialize_database

LOG = logging.getLogger('load-speed')
REPO = Path(__file__).resolve().parents[2]
CALIBRATION_SCALES = {'pagila': 32, 'pagila-htap': 32, 'imdb': 0.5}


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return number


def positive_integer(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--profiles',
        nargs='+',
        choices=CALIBRATION_SCALES,
        default=['pagila', 'imdb'],
        help='profiles in execution order',
    )
    parser.add_argument(
        '--target-gib',
        type=positive_number,
        default=1,
        help='target tables plus indexes size in GiB (default: 1)',
    )
    parser.add_argument('--workers', type=positive_integer, default=4)
    parser.add_argument('--batch-rows', type=positive_integer, default=100_000)
    parser.add_argument(
        '--timeout',
        type=positive_number,
        default=1800,
        help='timeout of each loader operation, in seconds',
    )
    parser.add_argument('--image', default='postgres:18', help='PostgreSQL Docker image')
    parser.add_argument(
        '--output', type=Path, help='new output directory (default: a temporary directory)'
    )
    args = parser.parse_args(argv)
    if len(set(args.profiles)) != len(args.profiles):
        parser.error('--profiles must not contain duplicates')
    return args


def save_summary(root, results):
    selected = [row for row in results if row.get('selected')]
    with (root / 'summary.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                'profile',
                'scale',
                'relations_bytes',
                'database_bytes',
                'elapsed_seconds',
                'data_seconds',
                'indexes_seconds',
            ]
        )
        for row in selected:
            writer.writerow(
                [
                    row['profile'],
                    row['scale'],
                    row['relations_bytes'],
                    row['database_bytes'],
                    row['elapsed_seconds'],
                    row['phases_seconds']['data'],
                    row['phases_seconds']['indexes'],
                ]
            )
    lines = [
        '# Initialization speed',
        '',
        '| Profile | Tables + indexes, GiB | Insert, s | Indexes, s | Full preparation, s |',
        '| --- | ---: | ---: | ---: | ---: |',
    ]
    for row in selected:
        phases = row['phases_seconds']
        lines.append(
            f'| {row["profile"]} | {row["relations_bytes"] / 1024**3:.3f} | '
            f'{phases["data"]:.3f} | {phases["indexes"]:.3f} | {row["elapsed_seconds"]:.3f} |'
        )
    lines += [
        '',
        'Full preparation includes LOGGED conversion, constraints, VACUUM/ANALYZE, '
        'fsync restoration, CHECKPOINT/sync and replay of preparation WAL by both replicas.',
        'Container/database creation, calibration, validation queries and pgbench are excluded.',
        'One selected measurement per profile, after calibration; OS caches are not cleared.',
        'See results.json for every attempt and phase; '
        'environment.json records the stand settings.',
    ]
    (root / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def wait_ready(container):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        container.reload()
        if container.status in {'exited', 'dead'}:
            raise RuntimeError('Disposable PostgreSQL node exited: ' + container.name)
        if container.exec_run(['pg_isready', '-h', '127.0.0.1']).exit_code == 0:
            return
        time.sleep(0.2)
    raise RuntimeError('Disposable PostgreSQL node did not start: ' + container.name)


async def run(primary, conf, args, root, disk_path):
    target = args.target_gib * 1024**3
    options = LoadOptions(workers=args.workers, batch_rows=args.batch_rows, timeout=args.timeout)
    transport = DockerConnection({'container_name': primary.name}, {})
    await transport.start()
    admin = None
    results = []
    try:
        admin = await asyncpg.connect(**conf, server_settings={'synchronous_commit': 'off'})
        for _ in range(300):
            if (
                await admin.fetchval(
                    "SELECT count(*) FROM pg_stat_replication WHERE state='streaming'"
                )
                == 2
            ):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError('Two physical replicas did not start streaming')
        await admin.execute(
            "ALTER SYSTEM SET synchronous_standby_names='ANY 2 (load_replica1, load_replica2)'"
        )
        await admin.execute("ALTER SYSTEM SET synchronous_commit='remote_apply'")
        await admin.fetchval('SELECT pg_reload_conf()')
        for _ in range(100):
            if (
                await admin.fetchval(
                    "SELECT count(*) FROM pg_stat_replication WHERE sync_state='quorum'"
                )
                == 2
            ):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError('Two synchronous replicas did not become ready')

        settings = dict(
            await admin.fetchrow(
                "SELECT version() AS version, current_setting('shared_buffers') AS shared_buffers, "
                "current_setting('maintenance_work_mem') AS maintenance_work_mem, "
                "current_setting('work_mem') AS work_mem, "
                "current_setting('max_wal_size') AS max_wal_size, "
                "current_setting('max_parallel_maintenance_workers') "
                'AS max_parallel_maintenance_workers, '
                "current_setting('synchronous_standby_names') AS synchronous_standby_names"
            )
        )
        metadata = {
            'started_at': datetime.now(timezone.utc).isoformat(),
            'target_relation_bytes': target,
            'options': asdict(options),
            'postgresql': settings,
            'topology': 'one primary and two synchronous physical replicas, same Docker host',
            'image': primary.image.id,
            'disk_free_before_bytes': shutil.disk_usage(disk_path).free,
            'disk_path': str(disk_path),
            'cpu': subprocess.check_output(['lscpu'], text=True),
            'memory': subprocess.check_output(['free', '-b'], text=True),
            'git_head': subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True
            ).strip(),
            'source_uncommitted': bool(
                subprocess.check_output(
                    ['git', 'status', '--porcelain'], cwd=REPO, text=True
                ).strip()
            ),
            'invocation': vars(args) | {'output': str(root)},
        }
        (root / 'environment.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')

        async def measure(profile, scale, kind):
            if shutil.disk_usage(disk_path).free < max(1024**3, 7 * target):
                raise RuntimeError(
                    'Insufficient free disk space for the next disposable measurement'
                )
            database = 'speed_' + profile.replace('-', '_') + '_' + kind
            await admin.execute('CREATE DATABASE ' + database)
            db_conf = {**conf, 'database': database}
            plan = load_plan(WORKLOAD_PROFILES_PATH / profile, 'generator.py', scale)
            wal_before = await admin.fetchval('SELECT pg_current_wal_insert_lsn()::text')
            LOG.info('MEASURE START profile=%s kind=%s scale=%s', profile, kind, scale)
            started = time.perf_counter()
            evidence = await initialize_database(
                LOG, plan, db_conf, options, ConnectionType.DOCKER, transport
            )
            elapsed = time.perf_counter() - started
            db = await asyncpg.connect(**db_conf)
            try:
                size = dict(
                    await db.fetchrow(
                        'SELECT pg_database_size(current_database()) AS database_bytes, '
                        'sum(pg_total_relation_size(c.oid))::bigint AS relations_bytes, '
                        'sum(pg_table_size(c.oid))::bigint AS tables_bytes, '
                        'sum(pg_indexes_size(c.oid))::bigint AS indexes_bytes '
                        'FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                        "WHERE n.nspname=ANY($1::text[]) AND c.relkind IN ('r','m')",
                        list(plan.schemas),
                    )
                )
                assert evidence['fsync_after'] == 'on'
                assert await db.fetchval('SHOW synchronous_commit') == 'remote_apply'
                assert await db.fetchval('SELECT count(*) FROM pg_index WHERE NOT indisvalid') == 0
                assert (
                    await db.fetchval('SELECT count(*) FROM pg_constraint WHERE NOT convalidated')
                    == 0
                )
                assert (
                    await db.fetchval(
                        'SELECT count(*) FROM pg_class c '
                        'JOIN pg_namespace n ON n.oid=c.relnamespace '
                        "WHERE n.nspname=ANY($1::text[]) AND relkind='r' AND relpersistence<>'p'",
                        list(plan.schemas),
                    )
                    == 0
                )
            finally:
                await db.close()
            barrier = evidence['replication_barrier']
            replicas = [
                dict(row)
                for row in await admin.fetch(
                    'SELECT application_name, state, sync_state, replay_lsn::text, '
                    'pg_wal_lsn_diff($1::text::pg_lsn, replay_lsn)::bigint '
                    'AS preparation_lag_bytes FROM pg_stat_replication ORDER BY application_name',
                    barrier['target_lsn'],
                )
            ]
            assert len(replicas) == 2 and barrier['replicas'] == 2
            assert all(
                row['state'] == 'streaming'
                and row['sync_state'] == 'quorum'
                and row['preparation_lag_bytes'] is not None
                and row['preparation_lag_bytes'] <= 0
                for row in replicas
            )
            for row in replicas:
                row['preparation_lag_bytes'] = max(0, row['preparation_lag_bytes'])
            phase_totals = {}
            for phase in evidence['phases']:
                name = phase['name'].split(':')[0]
                phase_totals[name] = phase_totals.get(name, 0) + phase['elapsed_seconds']
            phase_totals['replica_replay'] = barrier['elapsed_seconds']
            result = {
                'profile': profile,
                'kind': kind,
                'scale': scale,
                'elapsed_seconds': elapsed,
                **size,
                'phases_seconds': phase_totals,
                'rows_inserted': sum(
                    t['rows'] for p in evidence['phases'] if p['name'] == 'data' for t in p['tasks']
                ),
                'wal_bytes': await admin.fetchval(
                    'SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), $1::text::pg_lsn)::bigint',
                    wal_before,
                ),
                'replicas': replicas,
                'evidence': evidence,
            }
            (root / (profile + '-' + kind + '.json')).write_text(
                json.dumps(result, indent=2), encoding='utf-8'
            )
            results.append(result)
            (root / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
            LOG.info(
                'MEASURE FINISHED %s',
                json.dumps({k: v for k, v in result.items() if k != 'evidence'}),
            )
            # Release this dataset on all members before preparing the next one.
            await admin.execute('DROP DATABASE ' + database)
            cleanup_lsn = await admin.fetchval('SELECT pg_current_wal_insert_lsn()::text')
            for _ in range(600):
                if (
                    await admin.fetchval(
                        'SELECT count(*) FROM pg_stat_replication '
                        'WHERE replay_lsn >= $1::text::pg_lsn',
                        cleanup_lsn,
                    )
                    == 2
                ):
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError('Replicas did not replay dataset cleanup')
            await admin.execute('CHECKPOINT')
            return result

        with patch('pg_perf_bench.initialization_settings._STATE_DIR', root / 'state'):
            for profile in args.profiles:
                sample_scale = CALIBRATION_SCALES[profile] * min(1, args.target_gib * 4)
                sample = await measure(profile, sample_scale, 'calibration')
                scale = sample_scale * target * 1.02 / sample['relations_bytes']
                for attempt in range(1, 4):
                    measured = await measure(profile, scale, f'target_{attempt}')
                    if 0.98 * target <= measured['relations_bytes'] <= 1.07 * target:
                        measured['selected'] = True
                        (root / 'results.json').write_text(
                            json.dumps(results, indent=2), encoding='utf-8'
                        )
                        save_summary(root, results)
                        break
                    scale *= target * 1.01 / measured['relations_bytes']
                else:
                    raise RuntimeError(
                        f'{profile}: target size was not reached after three attempts; '
                        'the requested size may be below the profile minimum. '
                        f'See {root / "results.json"}'
                    )
    finally:
        if admin is not None:
            await admin.close()
        await transport.aclose()


def provision_and_run(args, root):
    with ExitStack() as cleanup:
        client = docker.from_env()
        cleanup.callback(client.close)
        disk_path = Path(client.info()['DockerRootDir'])
        if not disk_path.is_dir():
            raise RuntimeError('This benchmark requires a local Linux Docker daemon')
        tag = uuid.uuid4().hex[:10]
        network = client.networks.create('pg-perf-speed-' + tag)
        cleanup.callback(network.remove)
        password = uuid.uuid4().hex
        primary = client.containers.run(
            args.image,
            ['postgres'],
            name='pg-perf-speed-primary-' + tag,
            environment={'POSTGRES_PASSWORD': password},
            network=network.name,
            ports={'5432/tcp': ('127.0.0.1', None)},
            shm_size='512m',
            detach=True,
        )
        cleanup.callback(primary.remove, force=True, v=True)
        wait_ready(primary)
        primary.reload()
        port = int(primary.attrs['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
        hba = primary.exec_run(
            [
                'sh',
                '-ec',
                'echo "host replication postgres all scram-sha-256" >> "$PGDATA/pg_hba.conf"; '
                'psql -U postgres -c "SELECT pg_reload_conf()"',
            ],
            user='postgres',
        )
        assert hba.exit_code == 0
        for i in (1, 2):
            standby = client.containers.run(
                args.image,
                [
                    'sh',
                    '-ec',
                    'mkdir -p "$PGDATA"; chmod 700 "$PGDATA"; '
                    f'pg_basebackup -D "$PGDATA" -R -X stream -C -S load_replica{i}; '
                    'exec postgres -D "$PGDATA"',
                ],
                name=f'pg-perf-speed-replica{i}-' + tag,
                user='postgres',
                network=network.name,
                shm_size='512m',
                environment={
                    'PGHOST': primary.name,
                    'PGUSER': 'postgres',
                    'PGPASSWORD': password,
                    'PGAPPNAME': f'load_replica{i}',
                },
                detach=True,
            )
            cleanup.callback(standby.remove, force=True, v=True)
            wait_ready(standby)
        LOG.info('Artifact directory: %s', root)
        conf = dict(
            host='127.0.0.1', port=port, user='postgres', password=password, database='postgres'
        )
        asyncio.run(run(primary, conf, args, root, disk_path))
    LOG.info('Disposable containers removed; artifacts: %s', root)


def main(argv=None):
    args = parse_args(argv)
    if args.output is None:
        root = Path(tempfile.mkdtemp(prefix='pg-perf-load-speed-'))
    else:
        root = args.output.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=False)
    logfile = logging.FileHandler(root / 'progress.log', encoding='utf-8')
    try:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(message)s',
            handlers=[logfile, logging.StreamHandler(sys.stdout)],
            force=True,
        )
        provision_and_run(args, root)
    except Exception:
        LOG.exception('Initialization speed test failed; artifacts: %s', root)
        raise
    finally:
        logfile.close()


if __name__ == '__main__':
    main()
