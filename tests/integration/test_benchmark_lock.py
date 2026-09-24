"""Lock regression tests on a fresh local server, never a benchmark cluster."""

import asyncio
import getpass
import logging
import os
import socket
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import LoadOptions, LoadPlan
from pg_perf_bench.initialization_settings import (
    InitializationSettings,
    close_initialization_settings,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get('PG_PERF_LOCAL_BIN'), reason='local PostgreSQL required'),
]


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp('benchmark-lock')
    binary = Path(os.environ['PG_PERF_LOCAL_BIN'])
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    subprocess.run(
        [
            str(binary / 'initdb'),
            '-D',
            str(root / 'data'),
            '-A',
            'trust',
            '--no-locale',
            '--encoding=UTF8',
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(binary / 'pg_ctl'),
            '-D',
            str(root / 'data'),
            '-l',
            str(root / 'server.log'),
            '-o',
            f'-p {port} -h 127.0.0.1 -k {root}',
            '-w',
            'start',
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield {'host': '127.0.0.1', 'port': port, 'user': getpass.getuser()}, root
    finally:
        subprocess.run(
            [str(binary / 'pg_ctl'), '-D', str(root / 'data'), '-m', 'fast', '-w', 'stop'],
            check=True,
            capture_output=True,
        )


def guard(conf, root, database):
    return InitializationSettings(
        logging.getLogger(__name__),
        conf,
        LoadOptions(fsync='keep'),
        'managed',
        None,
        control_database=database,
        state_dir=root / 'state',
    )


@pytest.mark.parametrize('holder_policy', ['skip', 'once'])
@pytest.mark.parametrize('contender_mode', ['schema', 'database-fast', 'database-legacy'])
def test_running_sweep_rejects_reset_before_terminating_any_connection(
    server, holder_policy, contender_mode
):
    async def scenario():
        conf, root = server
        conf = {**conf, 'database': 'guard_' + uuid.uuid4().hex}
        admin = await asyncpg.connect(**{**conf, 'database': 'postgres'})
        await admin.execute('CREATE DATABASE ' + conf['database'])
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def workload(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                db = await asyncpg.connect(**conf)
                try:
                    await db.execute(
                        'CREATE TABLE sentinel(n int); INSERT INTO sentinel VALUES(42)'
                    )
                finally:
                    await db.close()
                started.set()
                await release.wait()
            return {'status': 'completed', 'metrics': {'tps': 1}}

        config = {
            'init_policy': holder_policy,
            'init_mode': 'legacy',
            'managed': True,
            'allow_database_reset': holder_policy != 'skip',
            'pgbench_iter_list': [1, 2],
        }
        plan = LoadPlan(('app',), 'CREATE SCHEMA app', (), ())
        with (
            patch.object(BenchmarkRunner, 'run_benchmark_with_evidence', workload),
            patch('pg_perf_bench.benchmark.load_plan', return_value=plan),
            patch('pg_perf_bench.benchmark.check_workload_session', return_value=None),
        ):
            task = asyncio.create_task(
                BenchmarkRunner.run_benchmark_iterations(
                    logging.getLogger(__name__),
                    [['init', 'work']] * 2,
                    'managed',
                    None,
                    conf,
                    config,
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 10)
                contender = {
                    **config,
                    'init_policy': 'each-iteration',
                    'allow_database_reset': True,
                    'init_fsync': 'keep',
                    'init_mode': 'legacy' if contender_mode == 'database-legacy' else 'fast',
                    'reset_mode': 'schema' if contender_mode == 'schema' else 'database',
                    'workload_path': str(root),
                    'init_entrypoint': 'unused.py',
                }
                with pytest.raises(ConfigurationError, match='server lock'):
                    await BenchmarkRunner.run_benchmark_iterations(
                        logging.getLogger(__name__),
                        [['init', 'work']],
                        'managed',
                        None,
                        conf,
                        contender,
                    )
                assert not task.done()
                assert calls == 1  # The rejected run never reaches initialization/workload.
                db = await asyncpg.connect(**conf)
                try:
                    assert await db.fetchval('SELECT n FROM sentinel') == 42
                finally:
                    await db.close()
                release.set()
                assert len(await asyncio.wait_for(task, 10)) == 2
                # Every path released its locks; another controller may proceed.
                next_guard = guard(conf, root, 'postgres')
                await next_guard.open(recover_only=True)
                await close_initialization_settings(next_guard)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await admin.close()

    asyncio.run(scenario())


def test_cross_database_simultaneous_acquisition_and_lost_connection(server):
    async def scenario():
        conf, root = server
        conf = {**conf, 'database': 'race_' + uuid.uuid4().hex}
        admin = await asyncpg.connect(**{**conf, 'database': 'postgres'})
        await admin.execute('CREATE DATABASE ' + conf['database'])
        try:
            for _ in range(10):
                guards = [guard(conf, root, db) for db in ('postgres', conf['database'])]
                results = await asyncio.gather(
                    *(g.open(recover_only=True) for g in guards), return_exceptions=True
                )
                try:
                    assert sum(r is None for r in results) <= 1
                    assert all(r is None or isinstance(r, ConfigurationError) for r in results)
                finally:
                    for g in guards:
                        await close_initialization_settings(g)
            original = guard(conf, root, 'postgres')
            competitor = guard(conf, root, conf['database'])
            await original.open(recover_only=True)
            try:
                pid = await original.db.fetchval('SELECT pg_backend_pid()')
                await admin.fetchval('SELECT pg_terminate_backend($1, 5000)', pid)

                async def disconnected():
                    while not original.db.is_closed():
                        await asyncio.sleep(0.001)

                await asyncio.wait_for(disconnected(), 5)
                with pytest.raises(ConfigurationError):
                    await original.assert_held()
                await competitor.open(recover_only=True)
                # Reconnect after a planned restart must not bypass a new owner.
                with pytest.raises(ConfigurationError, match='server lock'):
                    await original.reopen_after_restart()
                await competitor.assert_held()
            finally:
                await close_initialization_settings(original)
                await close_initialization_settings(competitor)
        finally:
            await admin.close()

    asyncio.run(scenario())


def test_cross_database_lock_visibility_does_not_require_superuser(server):
    async def scenario():
        conf, root = server
        name = 'limited_' + uuid.uuid4().hex
        admin = await asyncpg.connect(**{**conf, 'database': 'postgres'})
        await admin.execute('CREATE ROLE ' + name + ' LOGIN')
        await admin.execute('CREATE DATABASE ' + name + ' OWNER ' + name)
        super_guard = guard({**conf, 'database': name}, root, 'postgres')
        limited_guard = guard({**conf, 'database': name, 'user': name}, root, name)
        try:
            await super_guard.open(recover_only=True)
            with pytest.raises(ConfigurationError, match='server lock'):
                await limited_guard.open(recover_only=True)
            await close_initialization_settings(super_guard)
            await limited_guard.open(recover_only=True)
            await limited_guard.assert_held()
            assert not await limited_guard.db.fetchval(
                "SELECT current_setting('is_superuser')::bool"
            )
            with pytest.raises(ConfigurationError, match='server lock'):
                await super_guard.open(recover_only=True)
        finally:
            await close_initialization_settings(super_guard)
            await close_initialization_settings(limited_guard)
            await admin.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('policy,expected', [('once', [1, 2]), ('each-iteration', [1, 1])])
def test_database_reset_reacquires_lock_after_real_postgresql_restart(server, policy, expected):
    from unittest.mock import AsyncMock, MagicMock

    async def scenario():
        conf, root = server
        conf = {**conf, 'database': 'restart_' + uuid.uuid4().hex}
        binary = Path(os.environ['PG_PERF_LOCAL_BIN'])
        lifecycle = MagicMock(sync=AsyncMock())

        async def start():
            status = subprocess.run(
                [str(binary / 'pg_ctl'), '-D', str(root / 'data'), 'status'], capture_output=True
            )
            if status.returncode:
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        str(binary / 'pg_ctl'),
                        '-D',
                        str(root / 'data'),
                        '-l',
                        str(root / 'server.log'),
                        '-o',
                        f'-p {conf["port"]} -h 127.0.0.1 -k {root}',
                        '-w',
                        'start',
                    ],
                    check=True,
                    capture_output=True,
                )

        async def stop():
            await asyncio.to_thread(
                subprocess.run,
                [str(binary / 'pg_ctl'), '-D', str(root / 'data'), '-m', 'fast', '-w', 'stop'],
                check=True,
                capture_output=True,
            )

        lifecycle.start_db = AsyncMock(side_effect=start)
        lifecycle.stop_db = AsyncMock(side_effect=stop)
        values = []

        async def workload(*args, **kwargs):
            controller = kwargs['initialization_settings']
            await controller.assert_held()
            assert controller.control_database == 'postgres'
            db = await asyncpg.connect(**conf)
            try:
                await db.execute(
                    'CREATE TABLE IF NOT EXISTS counter(n int); INSERT INTO counter VALUES(1)'
                )
                values.append(await db.fetchval('SELECT count(*) FROM counter'))
            finally:
                await db.close()
            return {'status': 'completed', 'metrics': {'tps': 1}}

        with (
            patch.object(BenchmarkRunner, 'run_benchmark_with_evidence', workload),
            patch('pg_perf_bench.benchmark.PatroniController.detect', AsyncMock(return_value=None)),
            patch(
                'pg_perf_bench.benchmark.get_conn_type_tasks', return_value=lambda **kw: lifecycle
            ),
        ):
            try:
                result = await BenchmarkRunner.run_benchmark_iterations(
                    logging.getLogger(__name__),
                    [['init', 'work']] * 2,
                    'local',
                    MagicMock(),
                    conf,
                    {
                        'init_policy': policy,
                        'init_mode': 'legacy',
                        'allow_database_reset': True,
                        'pgbench_iter_list': [1, 2],
                        'pg_data_path': str(root / 'data'),
                    },
                )
                assert len(result) == 2
                assert values == expected
                assert lifecycle.stop_db.await_count == (1 if policy == 'once' else 2)
            finally:
                await start()  # Ensure fixture cleanup can stop its local server after a failure.
        after = guard(conf, root, 'postgres')
        await after.open(recover_only=True)
        await close_initialization_settings(after)

    asyncio.run(scenario())
