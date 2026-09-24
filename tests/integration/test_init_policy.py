"""Local disposable PostgreSQL check; never connects to benchmark clusters."""

import asyncio
import logging
import os
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.errors import CommandExecutionError
from pg_perf_bench.initialization import LoadPlan, LoadTask

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get('PG_PERF_LOCAL_BIN'),
        reason='PG_PERF_LOCAL_BIN required for disposable local PostgreSQL',
    ),
]


def test_reset_once_and_skip_preserve_actual_rows(tmp_path):
    binary = Path(os.environ['PG_PERF_LOCAL_BIN'])
    data = tmp_path / 'data'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    subprocess.run(
        [str(binary / 'initdb'), '-D', str(data), '-A', 'trust', '--no-locale', '--encoding=UTF8'],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(binary / 'pg_ctl'),
            '-D',
            str(data),
            '-l',
            str(tmp_path / 'server.log'),
            '-o',
            f'-p {port} -h 127.0.0.1 -k {tmp_path}',
            '-w',
            'start',
        ],
        check=True,
        capture_output=True,
    )
    try:
        asyncio.run(check_policies(binary, port, tmp_path))
    finally:
        subprocess.run(
            [str(binary / 'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'],
            check=True,
            capture_output=True,
        )


async def check_policies(binary, port, root):
    conf = {
        'host': '127.0.0.1',
        'port': port,
        'user': os.environ['USER'],
        'database': 'policy_test',
    }
    admin = await asyncpg.connect(**{**conf, 'database': 'postgres'})
    await admin.execute('CREATE DATABASE policy_test')
    await admin.close()
    db = await asyncpg.connect(**conf)
    await db.execute(
        "CREATE TABLE public.sentinel (value text); INSERT INTO public.sentinel VALUES ('preserve')"
    )
    plan = LoadPlan(
        ('app',),
        'CREATE SCHEMA app; CREATE TABLE app.counter (n integer);',
        (LoadTask('counter', 'INSERT INTO app.counter VALUES (0)'),),
        (),
    )
    script = root / 'work.sql'
    script.write_text('UPDATE app.counter SET n=n+1;\n')
    config = {
        'init_mode': 'fast',
        'init_fsync': 'keep',
        'reset_mode': 'schema',
        'allow_database_reset': True,
        'managed': True,
        'workload_path': str(root),
        'init_entrypoint': 'unused.py',
        'pgbench_iter_name': 'pgbench_clients',
        'pgbench_iter_list': [1, 2, 3],
        'command_timeout': 20,
        'psql_path': os.environ.get('PG_PERF_LOCAL_PSQL', str(binary / 'psql')),
        'pgbench_path': str(binary / 'pgbench'),
    }
    commands = [
        [
            'common-loader',
            f'{binary}/pgbench -n -t 1 -c {n} -j {n} '
            f'-h 127.0.0.1 -p {port} -U {conf["user"]} -f {script} policy_test',
        ]
        for n in [1, 2, 3]
    ]
    try:
        for policy, expected in [('each-iteration', 3), ('once', 6), ('skip', 12)]:
            with patch('pg_perf_bench.benchmark.load_plan', return_value=plan):
                runs = await BenchmarkRunner.run_benchmark_iterations(
                    logging.getLogger(__name__),
                    commands,
                    'managed',
                    None,
                    conf,
                    {**config, 'init_policy': policy, 'allow_database_reset': policy != 'skip'},
                )
            assert await db.fetchval('SELECT n FROM app.counter') == expected
            assert await db.fetchval('SELECT value FROM public.sentinel') == 'preserve'
            assert len(runs) == 3
            assert all(run['metrics']['transactions'] == i for i, run in enumerate(runs, 1))
            assert [run['initialization_performed'] for run in runs] == {
                'each-iteration': [True, True, True],
                'once': [True, False, False],
                'skip': [False, False, False],
            }[policy]
            assert await db.fetchval('SELECT pg_try_advisory_lock(1346847301,1229867348)')
            await db.execute('SELECT pg_advisory_unlock_all()')
        # A failed workload must also release the lock that spans a reused dataset.
        script.write_text('SELECT missing_column FROM app.counter;\n')
        with patch('pg_perf_bench.benchmark.load_plan', return_value=plan):
            with pytest.raises(CommandExecutionError):
                await BenchmarkRunner.run_benchmark_iterations(
                    logging.getLogger(__name__),
                    commands,
                    'managed',
                    None,
                    conf,
                    {**config, 'init_policy': 'skip', 'allow_database_reset': False},
                )
        assert await db.fetchval('SELECT pg_try_advisory_lock(1346847301,1229867348)')
        await db.execute('SELECT pg_advisory_unlock_all()')
        assert await db.fetchval('SELECT n FROM app.counter') == 12
    finally:
        await db.close()
