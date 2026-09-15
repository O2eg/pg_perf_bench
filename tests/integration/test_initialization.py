"""Opt-in common-loader checks against disposable PostgreSQL containers."""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import replace
from unittest.mock import patch

import asyncpg
import docker
import pytest

from pg_perf_bench.connections.docker import DockerConnection
from pg_perf_bench.const import WORKLOAD_PROFILES_PATH, ConnectionType
from pg_perf_bench.initialization import (
    LoadOptions,
    LoadPlan,
    LoadTask,
    load_plan,
    prepare_database,
)
from pg_perf_bench.initialization_settings import InitializationSettings, initialize_database
from tests.integration.test_replication_report import postgres

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_INIT_INTEGRATION') != '1',
        reason='set PG_PERF_BENCH_INIT_INTEGRATION=1 for disposable loader tests',
    ),
]
LOGGER = logging.getLogger(__name__)


async def fingerprint(db, schema):
    result = {}
    tables = await db.fetch(
        'SELECT tablename FROM pg_tables WHERE schemaname=$1 ORDER BY tablename', schema
    )
    for row in tables:
        name = row['tablename']
        rows = await db.fetch(
            f"SELECT (to_jsonb(t) - 'last_update')::text AS value "
            f'FROM ONLY "{schema}"."{name}" t ORDER BY value'
        )
        result[name] = hashlib.sha256('\n'.join(row['value'] for row in rows).encode()).hexdigest()
    return result


@pytest.mark.parametrize('profile', ['pagila', 'pagila-htap', 'imdb'])
def test_profiles_batch_sizes_server_versions_and_all_workload_scripts(profile):
    expected = None
    root = WORKLOAD_PROFILES_PATH / profile
    for major in (10, 18):
        with postgres(major) as (_, conf):

            async def scenario():
                db = await asyncpg.connect(**conf)
                try:
                    checksums = []
                    for workers, batch in ((1, 100_000), (4, 137)):
                        plan = load_plan(root, 'generator.py', 0.01)
                        await db.execute(f'DROP SCHEMA IF EXISTS {plan.schemas[0]} CASCADE')
                        result = await prepare_database(
                            LOGGER, plan, conf, LoadOptions(workers=workers, batch_rows=batch)
                        )
                        assert (
                            await db.fetchval(
                                'SELECT count(*) FROM pg_class c JOIN pg_namespace n '
                                "ON n.oid=c.relnamespace WHERE nspname=$1 AND relkind='r' "
                                "AND relpersistence != 'p'",
                                plan.schemas[0],
                            )
                            == 0
                        )
                        assert (
                            await db.fetchval('SELECT count(*) FROM pg_index WHERE NOT indisvalid')
                            == 0
                        )
                        assert (
                            await db.fetchval(
                                'SELECT count(*) FROM pg_constraint WHERE NOT convalidated'
                            )
                            == 0
                        )
                        assert next(p for p in result['phases'] if p['name'] == 'indexes')['tasks']
                        checksums.append(await fingerprint(db, plan.schemas[0]))
                    assert checksums[0] == checksums[1]
                    return checksums[0]
                finally:
                    await db.close()

            actual = asyncio.run(scenario())
            if expected is None:
                expected = actual
            assert actual == expected
            env = {
                **os.environ,
                'PGOPTIONS': '-c search_path='
                + ('imdb' if profile == 'imdb' else 'pagila')
                + ',public',
                **{
                    key: str(conf[value])
                    for key, value in (
                        ('PGHOST', 'host'),
                        ('PGPORT', 'port'),
                        ('PGUSER', 'user'),
                        ('PGDATABASE', 'database'),
                        ('PGPASSWORD', 'password'),
                    )
                },
            }
            manifest = json.loads((root / 'profile.json').read_text())
            for relative_path in manifest['files']['queries']:
                query = root / relative_path
                result = subprocess.run(
                    [
                        '/usr/lib/postgresql/18/bin/pgbench',
                        '-n',
                        '-M',
                        'prepared',
                        '--random-seed=42',
                        '-c',
                        '2',
                        '-j',
                        '2',
                        '-t',
                        '2',
                        '-f',
                        str(query),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert result.returncode == 0, (major, profile, query.name, result.stderr)
                assert 'number of transactions actually processed: 4/4' in result.stdout
                assert 'number of failed transactions: 0' in result.stdout


@pytest.mark.parametrize('override', [None, 'on', 'off'])
def test_fsync_exact_restore_recovery_and_failure(tmp_path, override):
    with postgres(18) as (container, conf):

        async def scenario():
            transport = DockerConnection({'container_name': container.name}, {})
            await transport.start()
            db = await asyncpg.connect(**conf)
            options = LoadOptions(batch_rows=2, synchronous_commit='off')

            def factory(*args, **kwargs):
                return InitializationSettings(*args, **kwargs, state_dir=tmp_path)

            try:
                if override:
                    await db.execute(f'ALTER SYSTEM SET fsync = {override}')
                    await db.fetchval('SELECT pg_reload_conf()')
                    for _ in range(50):
                        if await db.fetchval('SHOW fsync') == override:
                            break
                        await asyncio.sleep(0.1)
                original = await db.fetchval('SHOW fsync')
                plan = LoadPlan(
                    ('loader',),
                    'CREATE SCHEMA loader; CREATE TABLE loader.t '
                    '(id bigint, fsync text, commit_policy text, indexes integer)',
                    (
                        LoadTask(
                            't',
                            "INSERT INTO loader.t SELECT g, current_setting('fsync'), "
                            "current_setting('synchronous_commit'), "
                            "(SELECT count(*) FROM pg_index WHERE indrelid='loader.t'::regclass) "
                            'FROM generate_series($1::bigint, $2::bigint) g',
                            count=7,
                        ),
                    ),
                    (LoadTask('pkey', 'CREATE UNIQUE INDEX t_pkey ON loader.t (id)'),),
                )
                with patch('pg_perf_bench.initialization_settings.InitializationSettings', factory):
                    evidence = await initialize_database(
                        LOGGER, plan, conf, options, ConnectionType.DOCKER, transport
                    )
                assert evidence['fsync_after'] == original
                assert await db.fetchval('SHOW synchronous_commit') == 'on'
                assert (
                    await db.fetchval(
                        "SELECT count(*) FROM loader.t WHERE fsync='off' "
                        "AND commit_policy='off' AND indexes=0"
                    )
                    == 7
                )
                assert not list(tmp_path.glob('*.json'))

                # A disconnected loader leaves a durable journal. Opening the
                # next run restores its exact prior override before any reset.
                guard = factory(LOGGER, conf, options, ConnectionType.DOCKER, transport)
                await guard.open()
                await guard.disable()
                assert list(tmp_path.glob('*.json'))
                await guard.db.close()
                recovered = factory(LOGGER, conf, options, ConnectionType.DOCKER, transport)
                await recovered.open()
                assert await db.fetchval('SHOW fsync') == original
                assert await recovered._auto_value() == override
                await recovered.close()
                assert not list(tmp_path.glob('*.json'))

                await db.execute('DROP SCHEMA loader CASCADE')
                broken = replace(plan, data=(LoadTask('broken', 'SELECT 1/0'),))
                with (
                    patch('pg_perf_bench.initialization_settings.InitializationSettings', factory),
                    pytest.raises(asyncpg.DivisionByZeroError),
                ):
                    await initialize_database(
                        LOGGER, broken, conf, options, ConnectionType.DOCKER, transport
                    )
                assert await db.fetchval('SHOW fsync') == original
                assert not list(tmp_path.glob('*.json'))
                await db.execute('DROP SCHEMA loader CASCADE')
                slow = replace(plan, data=(LoadTask('cancel', 'SELECT pg_sleep(30)'),))
                with patch('pg_perf_bench.initialization_settings.InitializationSettings', factory):
                    pending = asyncio.create_task(
                        initialize_database(
                            LOGGER, slow, conf, options, ConnectionType.DOCKER, transport
                        )
                    )
                    try:
                        for _ in range(100):
                            if await db.fetchval(
                                'SELECT count(*) FROM pg_stat_activity '
                                "WHERE application_name='pg_perf_bench:init' "
                                "AND query='SELECT pg_sleep(30)' AND state='active'"
                            ):
                                break
                            await asyncio.sleep(0.05)
                        else:
                            pytest.fail('loader did not start the cancellable task')
                        pending.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                    finally:
                        if not pending.done():
                            pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                assert await db.fetchval('SHOW fsync') == original
                assert not list(tmp_path.glob('*.json'))
            finally:
                await db.close()
                await transport.aclose()

        asyncio.run(scenario())


def test_two_synchronous_replicas_do_not_block_load_but_must_replay_before_return():
    with postgres(18) as (primary, conf):
        client = docker.from_env()
        standbys = []
        try:
            result = primary.exec_run(
                [
                    'sh',
                    '-c',
                    'echo "host replication postgres 0.0.0.0/0 md5" >> "$PGDATA/pg_hba.conf"',
                ],
                user='postgres',
            )
            assert result.exit_code == 0
            primary.exec_run(
                ['psql', '-U', 'postgres', '-c', 'SELECT pg_reload_conf()'], user='postgres'
            )
            primary.reload()
            address = primary.attrs['NetworkSettings']['Networks']['bridge']['IPAddress']
            for index in (1, 2):
                standby = client.containers.run(
                    'postgres:18',
                    [
                        'sh',
                        '-ec',
                        'mkdir -m 700 /tmp/standbydata; '
                        'pg_basebackup -D /tmp/standbydata -R -X stream; '
                        "exec postgres -D /tmp/standbydata -c listen_addresses='*'",
                    ],
                    name='pg-perf-init-standby-' + uuid.uuid4().hex[:10],
                    user='postgres',
                    environment={
                        'PGHOST': address,
                        'PGUSER': 'postgres',
                        'PGPASSWORD': conf['password'],
                        'PGAPPNAME': f'loader_replica{index}',
                    },
                    ports={'5432/tcp': ('127.0.0.1', None)},
                    detach=True,
                )
                standbys.append(standby)
            for standby in standbys:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if standby.exec_run(['pg_isready', '-h', '127.0.0.1']).exit_code == 0:
                        break
                    time.sleep(0.2)
                else:
                    pytest.fail(standby.logs(tail=30).decode())
                standby.reload()

            async def scenario():
                replicas = []
                db = await asyncpg.connect(**conf)
                transport = DockerConnection({'container_name': primary.name}, {})
                await transport.start()
                pending = None
                try:
                    for standby in standbys:
                        port = int(
                            standby.attrs['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
                        )
                        replicas.append(await asyncpg.connect(**{**conf, 'port': port}))
                    await db.execute('SET synchronous_commit=off')
                    await db.execute(
                        'SELECT pg_create_logical_replication_slot('
                        "'loader_logical', 'test_decoding')"
                    )
                    primary.exec_run(
                        [
                            'pg_recvlogical',
                            '-d',
                            'benchdb',
                            '-U',
                            'postgres',
                            '--slot=loader_logical',
                            '--start',
                            '-f',
                            '/dev/null',
                            '--no-loop',
                        ],
                        user='postgres',
                        environment={'PGAPPNAME': 'logical_reader'},
                        detach=True,
                    )
                    await db.execute('ALTER DATABASE benchdb SET synchronous_commit=remote_apply')
                    policy = 'ANY 2 (loader_replica1, loader_replica2)'
                    await db.execute("ALTER SYSTEM SET synchronous_standby_names='" + policy + "'")
                    await db.fetchval('SELECT pg_reload_conf()')
                    for _ in range(100):
                        if (
                            await db.fetchval('SHOW synchronous_standby_names') == policy
                            and await db.fetchval(
                                "SELECT count(*) FROM pg_stat_replication WHERE state='streaming' "
                                "AND application_name LIKE 'loader_replica%'"
                            )
                            == 2
                        ):
                            break
                        await asyncio.sleep(0.1)
                    else:
                        pytest.fail('two replicas did not become ready')
                    for _ in range(100):
                        if await db.fetchval(
                            'SELECT active FROM pg_replication_slots '
                            "WHERE slot_name='loader_logical'"
                        ):
                            break
                        await asyncio.sleep(0.05)
                    else:
                        pytest.fail('logical WAL consumer did not connect')
                    for replica in replicas:
                        await replica.execute('SELECT pg_wal_replay_pause()')
                    plan = LoadPlan(
                        ('loader',),
                        'CREATE SCHEMA loader; CREATE TABLE loader.t '
                        '(id bigint, commit_policy text)',
                        (
                            LoadTask(
                                'data',
                                'INSERT INTO loader.t SELECT g, '
                                "current_setting('synchronous_commit') "
                                'FROM generate_series($1::bigint, $2::bigint) g',
                                count=2000,
                            ),
                        ),
                        (LoadTask('pk', 'CREATE UNIQUE INDEX t_pk ON loader.t (id)'),),
                    )
                    pending = asyncio.create_task(
                        initialize_database(
                            LOGGER,
                            plan,
                            conf,
                            LoadOptions(batch_rows=137, synchronous_commit='off'),
                            ConnectionType.DOCKER,
                            transport,
                        )
                    )
                    for _ in range(200):
                        if pending.done():
                            pytest.fail(
                                f'initializer returned before replica replay: {pending.result()}'
                            )
                        if await db.fetchval(
                            'SELECT count(*) FROM pg_stat_activity '
                            "WHERE application_name='pg_perf_bench:init' "
                            "AND query LIKE '%replay_lsn >=%'"
                        ):
                            break
                        await asyncio.sleep(0.05)
                    else:
                        pytest.fail('load did not finish with replica replay paused')
                    assert (
                        await db.fetchval("SELECT count(*) FROM loader.t WHERE commit_policy='off'")
                        == 2000
                    )
                    assert await db.fetchval('SHOW fsync') == 'on'
                    assert await db.fetchval('SHOW synchronous_standby_names') == policy
                    await replicas[0].execute('SELECT pg_wal_replay_resume()')
                    await asyncio.sleep(0.3)
                    assert not pending.done()
                    await replicas[1].execute('SELECT pg_wal_replay_resume()')
                    result = await asyncio.wait_for(pending, 15)
                    assert result['replication_barrier']['replicas'] == 2
                    for replica in replicas:
                        assert await replica.fetchval('SELECT count(*) FROM loader.t') == 2000
                        assert await replica.fetchval('SHOW fsync') == 'on'
                    workload = await asyncpg.connect(**conf)
                    try:
                        assert await workload.fetchval('SHOW synchronous_commit') == 'remote_apply'
                        await workload.execute(
                            "INSERT INTO loader.t VALUES (3000, 'workload')", timeout=10
                        )
                    finally:
                        await workload.close()
                finally:
                    if pending is not None and not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    for replica in replicas:
                        await replica.close()
                    await db.close()
                    await transport.aclose()

            asyncio.run(scenario())
        finally:
            for standby in standbys:
                standby.remove(force=True, v=True)
            client.close()
