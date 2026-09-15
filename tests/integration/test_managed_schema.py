"""Full managed CLI runs against disposable servers with restricted SQL roles."""

import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.const import WORKLOAD_PROFILES_PATH
from pg_perf_bench.db_operations.db import DBTasks
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import LoadOptions, LoadPlan, LoadTask, quote_identifier
from pg_perf_bench.initialization_settings import InitializationSettings
from tests.integration.test_replication_report import postgres

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_INIT_INTEGRATION') != '1',
        reason='set PG_PERF_BENCH_INIT_INTEGRATION=1 for disposable loader tests',
    ),
]
LOGGER = logging.getLogger(__name__)
PASSWORD = 'disposable-managed-role'


async def restrict_server(conf):
    db = await asyncpg.connect(**conf)
    try:
        await db.execute(
            f"CREATE ROLE bench_owner LOGIN NOSUPERUSER NOCREATEDB PASSWORD '{PASSWORD}'"
        )
        await db.execute('ALTER DATABASE benchdb OWNER TO bench_owner')
        await db.execute('REVOKE CONNECT ON DATABASE postgres, template1 FROM PUBLIC')
        await db.execute('ALTER DATABASE benchdb SET search_path = public')
        await db.execute('ALTER ROLE bench_owner SET search_path = pg_catalog')
        await db.execute('CREATE SCHEMA unrelated AUTHORIZATION bench_owner')
        await db.execute(
            'CREATE TABLE unrelated.marker (id integer); INSERT INTO unrelated.marker VALUES (42)'
        )
        await db.execute('ALTER TABLE unrelated.marker OWNER TO bench_owner')
        await db.execute("ALTER SYSTEM SET log_connections = 'on'")
        await db.execute("ALTER SYSTEM SET log_line_prefix = '%u@%d '")
        await db.execute('SELECT pg_reload_conf()')
    finally:
        await db.close()
    return {**conf, 'user': 'bench_owner', 'password': PASSWORD}


async def identity(conf):
    db = await asyncpg.connect(**conf)
    try:
        return {
            'database': tuple(
                await db.fetchrow(
                    'SELECT oid, datdba, datacl::text FROM pg_database '
                    'WHERE datname=current_database()'
                )
            ),
            'settings': [
                tuple(row)
                for row in await db.fetch(
                    'SELECT setdatabase, setrole, setconfig::text FROM pg_db_role_setting '
                    'ORDER BY setdatabase, setrole'
                )
            ],
            'marker': await db.fetchval('SELECT id FROM unrelated.marker'),
            'fsync': await db.fetchval('SHOW fsync'),
        }
    finally:
        await db.close()


@pytest.mark.parametrize('major', [10, 18])
@pytest.mark.parametrize(
    ('profile', 'scale'), [('pagila', '1.15'), ('pagila-htap', '1.15'), ('imdb', '0.03')]
)
def test_full_managed_schema_cli_preserves_database_and_settings(tmp_path, major, profile, scale):
    with postgres(major) as (container, conf):
        limited = asyncio.run(restrict_server(conf))
        original = asyncio.run(identity(conf))
        cli = [
            sys.executable,
            '-m',
            'pg_perf_bench',
            'benchmark',
            '--managed',
            '--host',
            limited['host'],
            '--port',
            str(limited['port']),
            '--user',
            limited['user'],
            '--database',
            limited['database'],
            '--allow-database-reset',
            '--reset-mode',
            'schema',
            '--init-fsync',
            'keep',
            '--workload-profile',
            profile,
            '--workload-scale',
            scale,
            '--init-batch-rows',
            '1000',
            '--pgbench-clients',
            '1,2',
            '--workload-duration-seconds',
            '1',
            '--command-timeout',
            '90',
            '--output-dir',
            str(tmp_path),
            '--log-dir',
            str(tmp_path / 'logs'),
        ]
        counts = []
        for attempt in range(2):
            name = f'{profile}-{attempt}'
            result = subprocess.run(
                [*cli, '--report-name', name],
                capture_output=True,
                text=True,
                timeout=180,
                env={
                    **os.environ,
                    'PGPASSWORD': PASSWORD,
                    'PGSSLMODE': 'disable',
                    'PGOPTIONS': '-c search_path=unrelated -c statement_timeout=90000',
                },
            )
            (tmp_path / f'{name}.log').write_text(result.stdout + result.stderr)
            # Restricted roles can leave optional diagnostic items unavailable (exit 5).
            assert result.returncode in (0, 5), result.stdout + result.stderr
            report = json.loads((tmp_path / f'{name}.json').read_text())
            for section_name, section in report['sections'].items():
                for key, item in section['reports'].items():
                    if item.get('collection_status') not in ('error', 'partial'):
                        continue
                    assert (
                        section_name == 'replication' and key == 'replication_subscriptions'
                    ) or (section_name == 'storage' and key.endswith('_database_sizes')), (
                        section_name,
                        key,
                        item.get('reason'),
                    )
            assert (tmp_path / f'{name}.html').stat().st_size > 10000
            assert report['invocation']['managed_postgresql']
            assert 'managed_pg_info' not in report
            assert report['benchmark_methodology']['reset_mode'] == 'schema'
            assert not report['benchmark_methodology']['database_recreated_before_each_iteration']
            assert not report['benchmark_methodology']['server_restarted_before_each_iteration']
            assert (
                report['environment_evidence']['system_metrics_collection_scope']
                == 'unavailable_managed_postgresql'
            )
            for run in report['benchmark_runs']:
                assert run['metrics']['tps'] > 0
                assert run['metrics']['failed_transactions_percent'] == 0
                init = run['initialization']
                assert init['fsync_after'] == 'on'
                assert init['replication_barrier']['replicas'] == 0
                counts.append(next(p['tasks'] for p in init['phases'] if p['name'] == 'data'))
                for snapshot in run['storage'].values():
                    assert snapshot['reports']['table_sizes']['collection_status'] == 'ok'
            assert asyncio.run(identity(conf)) == original
        row_counts = [
            [(task['name'], task['rows'], task['batches']) for task in tasks] for tasks in counts
        ]
        assert all(count == row_counts[0] for count in row_counts)
        logs = container.logs().decode()
        assert 'bench_owner@postgres' not in logs
        assert 'bench_owner@template1' not in logs


def test_schema_preflight_ownership_probe_and_lock(tmp_path):
    with postgres(18) as (_, conf):

        async def scenario():
            limited = await restrict_server(conf)
            db = await asyncpg.connect(**conf)
            guard = InitializationSettings(
                LOGGER,
                limited,
                LoadOptions(fsync='keep'),
                'managed',
                None,
                control_database='benchdb',
                state_dir=tmp_path,
            )
            other = InitializationSettings(
                LOGGER,
                limited,
                LoadOptions(fsync='keep'),
                'managed',
                None,
                control_database='benchdb',
                state_dir=tmp_path,
            )
            try:
                await db.execute('CREATE SCHEMA pagila; CREATE TABLE pagila.preserved (id integer)')
                await guard.open()
                tasks = DBTasks(limited, LOGGER)
                with pytest.raises(ConfigurationError, match='ownership'):
                    await tasks.check_schema_reset(guard.db, ('pagila',), table_mode='unlogged')
                assert await db.fetchval("SELECT to_regclass('pagila.preserved')")
                with pytest.raises(ConfigurationError, match='Another initialization'):
                    await other.open()
                await db.execute('ALTER SCHEMA pagila OWNER TO bench_owner')
                await tasks.check_schema_reset(guard.db, ('pagila',), table_mode='unlogged')
                assert (
                    await db.fetchval(
                        "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'bench_probe_%'"
                    )
                    == 0
                )
                # WAL permission failures must stop before the existing schema is reset.
                await guard.close()
                await db.execute(
                    'REVOKE EXECUTE ON FUNCTION pg_current_wal_insert_lsn() FROM PUBLIC'
                )
                workload = {
                    'init_mode': 'fast',
                    'init_entrypoint': 'generator.py',
                    'workload_path': str(WORKLOAD_PROFILES_PATH / 'pagila'),
                    'workload_scale': 0.01,
                    'init_fsync': 'keep',
                    'reset_mode': 'schema',
                    'allow_database_reset': True,
                }
                with patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()) as reset:
                    with pytest.raises(ConfigurationError, match='before loading data'):
                        await BenchmarkRunner.run_benchmark_iterations(
                            LOGGER,
                            [['init', 'workload']],
                            'managed',
                            None,
                            limited,
                            workload,
                        )
                    reset.assert_not_awaited()
                assert await db.fetchval("SELECT to_regclass('pagila.preserved')")
            finally:
                await guard.close()
                await db.close()

        asyncio.run(scenario())


def test_failed_custom_load_retries_with_fresh_schema_and_connection_settings(tmp_path):
    with postgres(18) as (_, conf):

        async def scenario():
            limited = await restrict_server(conf)
            schema = 'bench "quoted space\\name'
            quoted = quote_identifier(schema)
            plan = LoadPlan(
                (schema,),
                f'CREATE SCHEMA {quoted}; CREATE TABLE {quoted}.data (id bigint, policy text); '
                f'CREATE TABLE {quoted}.checks (policy text)',
                (
                    LoadTask(
                        'data',
                        "INSERT INTO data SELECT g, current_setting('synchronous_commit') "
                        'FROM generate_series($1::bigint, $2::bigint) g',
                        count=7,
                    ),
                ),
                (LoadTask('pk', 'CREATE UNIQUE INDEX data_pk ON data (id)'),),
            )
            broken = replace(plan, data=(LoadTask('broken', 'SELECT 1/0'),))
            workload = {
                'init_mode': 'fast',
                'init_entrypoint': 'unused.py',
                'workload_path': str(tmp_path),
                'init_fsync': 'keep',
                'init_synchronous_commit': 'off',
                'reset_mode': 'schema',
                'allow_database_reset': True,
                'init_batch_rows': 2,
                'command_timeout': 20,
            }
            query = tmp_path / 'workload.sql'
            query.write_text("INSERT INTO checks VALUES (current_setting('synchronous_commit'));\n")
            command = shlex.join(
                [
                    '/usr/lib/postgresql/18/bin/pgbench',
                    '-n',
                    '-M',
                    'prepared',
                    '-c',
                    '1',
                    '-t',
                    '2',
                    '-f',
                    str(query),
                ]
            )
            with patch('pg_perf_bench.benchmark.load_plan', return_value=broken):
                with pytest.raises(asyncpg.DivisionByZeroError):
                    await BenchmarkRunner.run_benchmark_iterations(
                        LOGGER,
                        [['init', command]],
                        'managed',
                        None,
                        limited,
                        workload,
                    )
            # The failed run left its tables but released its lock. Reset must remove
            # those tables, and unusual schema names must survive libpq option parsing.
            from pg_perf_bench.benchmark import run_command_result

            async def check_lock_and_run(*args, **kwargs):
                competitor = InitializationSettings(
                    LOGGER,
                    limited,
                    LoadOptions(fsync='keep'),
                    'managed',
                    None,
                    control_database='benchdb',
                    state_dir=tmp_path / 'state',
                )
                with pytest.raises(ConfigurationError, match='Another initialization'):
                    await competitor.open()
                return await run_command_result(*args, **kwargs)

            with (
                patch('pg_perf_bench.benchmark.load_plan', return_value=plan),
                patch('pg_perf_bench.benchmark.run_command_result', side_effect=check_lock_and_run),
            ):
                results = await BenchmarkRunner.run_benchmark_iterations(
                    LOGGER,
                    [['init', command], ['init', command]],
                    'managed',
                    None,
                    limited,
                    workload,
                )
            assert all(r['metrics']['transactions'] == 2 for r in results)
            db = await asyncpg.connect(**limited)
            try:
                assert await db.fetchval(f'SELECT count(*) FROM {quoted}.data') == 7
                assert (
                    await db.fetchval(f"SELECT count(*) FROM {quoted}.data WHERE policy='off'") == 7
                )
                assert (
                    await db.fetchval(f"SELECT count(*) FROM {quoted}.checks WHERE policy='on'")
                    == 2
                )
                assert await db.fetchval('SHOW search_path') == 'pg_catalog'
                assert await db.fetchval('SHOW fsync') == 'on'
            finally:
                await db.close()

        asyncio.run(scenario())
