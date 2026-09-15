import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.const import WORKLOAD_PROFILES_PATH
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import LoadOptions, LoadPlan, LoadTask, load_plan, run_tasks
from pg_perf_bench.initialization_settings import InitializationSettings, initialize_database


def test_scheduler_bounds_transactions_and_resolves_reverse_zero_dependencies():
    active = 0
    peak = 0
    commits = []
    sessions = []

    class Connection:
        def __init__(self):
            self.job = None
            self.closed = False

        @asynccontextmanager
        async def transaction(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                yield
                commits.append(self.job)
            finally:
                active -= 1

        async def execute(self, sql, *params):
            if sql.startswith('SELECT set_config'):
                return 'SELECT 1'
            self.job = (sql, params)
            if sql == 'index':
                assert sum(job[0] == 'data' for job in commits) == 6
            await asyncio.sleep(0.001)
            return f'INSERT 0 {params[1] - params[0] + 1}' if params else 'CREATE INDEX'

        async def close(self):
            self.closed = True

    async def connect(**kwargs):
        assert kwargs['server_settings']['synchronous_commit'] == 'off'
        conn = Connection()
        sessions.append(conn)
        return conn

    async def scenario():
        with patch('pg_perf_bench.initialization.asyncpg.connect', connect):
            return await asyncio.wait_for(
                run_tasks(
                    MagicMock(),
                    (
                        LoadTask('index', 'index', depends_on=('data',)),
                        LoadTask('data', 'data', count=103, depends_on=('empty',)),
                        LoadTask('empty', 'never', count=0),
                    ),
                    {},
                    LoadOptions(workers=3, batch_rows=20),
                    phase='test',
                    search_path='public',
                ),
                2,
            )

    result = asyncio.run(scenario())
    assert peak == 3
    assert all(session.closed for session in sessions)
    assert result[1]['rows'] == 103 and result[1]['batches'] == 6
    ranges = sorted(params for sql, params in commits if sql == 'data')
    assert ranges == [(1, 20), (21, 40), (41, 60), (61, 80), (81, 100), (101, 103)]


@pytest.mark.parametrize('profile', ['pagila', 'pagila-htap', 'imdb'])
def test_bundled_load_plans_support_large_counts_without_materializing_batches(profile):
    plan = load_plan(WORKLOAD_PROFILES_PATH / profile, 'generator.py', 200_000)
    assert any(task.count and task.count > 2**31 for task in plan.data)
    assert len(plan.data) < 40
    assert all(task.count is None or '$1::bigint' in task.sql for task in plan.data)
    assert all(task.count is None for task in plan.indexes)
    assert plan.indexes and plan.constraints


@pytest.mark.parametrize('error', [RuntimeError('load failed'), asyncio.CancelledError()])
def test_failed_or_cancelled_load_restores_settings(error):
    guard = MagicMock()
    guard.open = AsyncMock()
    guard.disable = AsyncMock()
    guard.close = AsyncMock()
    guard.wait_for_replicas = AsyncMock()

    async def scenario():
        with (
            patch(
                'pg_perf_bench.initialization_settings.InitializationSettings', return_value=guard
            ),
            patch(
                'pg_perf_bench.initialization_settings.prepare_database',
                AsyncMock(side_effect=error),
            ),
            pytest.raises(type(error)),
        ):
            await initialize_database(MagicMock(), None, {}, LoadOptions(), 'local', object())

    asyncio.run(scenario())
    guard.disable.assert_awaited_once()
    guard.close.assert_awaited_once()
    guard.wait_for_replicas.assert_not_awaited()


def test_plan_validation_rejects_cycles_and_out_of_bigint_counts():
    from pg_perf_bench.initialization import validate_plan

    plan = LoadPlan(('bench',), 'CREATE SCHEMA bench', (), ())
    for tasks in (
        (
            LoadTask('a', 'SELECT 1', depends_on=('b',)),
            LoadTask('b', 'SELECT 1', depends_on=('a',)),
        ),
        (LoadTask('a', 'SELECT 1', count=2**63),),
    ):
        with pytest.raises(ConfigurationError):
            validate_plan(replace(plan, data=tasks))


def test_profile_loader_modes_and_execution_hash(tmp_path):
    import argparse
    import shutil

    from pg_perf_bench.benchmark import BenchmarkRunner
    from pg_perf_bench.config import build_runtime_config
    from pg_perf_bench.workloads import build_workload_evidence
    from tests.test_workload_profiles import _profile_runtime

    runtime = _profile_runtime('pagila')
    values = runtime.raw_args.copy()
    values['init_command'] = 'my-existing-loader --flag'
    legacy = build_runtime_config(argparse.Namespace(**values))
    assert legacy.workload.init_mode == 'legacy'
    assert legacy.workload.init_command == values['init_command']
    values['init_mode'] = 'fast'
    with pytest.raises(ConfigurationError, match='cannot be combined'):
        build_runtime_config(argparse.Namespace(**values))

    values = runtime.raw_args.copy()
    shutil.copytree(WORKLOAD_PROFILES_PATH / 'pagila', tmp_path / 'profile')
    values.update(
        workload_profile=None,
        workload_path=str(tmp_path / 'profile'),
        benchmark_type='custom',
        workload_command='pgbench -T 1',
        init_command=None,
    )
    custom = build_runtime_config(argparse.Namespace(**values))
    assert custom.workload.init_mode == 'fast' and custom.workload.init_entrypoint == 'generator.py'
    first = runtime.workload.as_legacy_dict(runtime.host)
    second = {**first, 'init_workers': 8}
    commands = BenchmarkRunner.load_iterations_config(runtime.database.as_legacy_dict(), first)
    evidence = build_workload_evidence(first, commands)
    changed = build_workload_evidence(second, commands)
    assert evidence['definition_hash'] == changed['definition_hash']
    assert evidence['execution_hash'] != changed['execution_hash']
    assert commands[0][0] == 'common-loader:generator.py'


def test_fast_preflight_failure_precedes_database_reset():
    from pg_perf_bench.benchmark import BenchmarkRunner

    async def scenario():
        guard = MagicMock(open=AsyncMock(side_effect=ConfigurationError('host unavailable')))
        with (
            patch('pg_perf_bench.benchmark.InitializationSettings', return_value=guard),
            patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()) as reset,
            pytest.raises(ConfigurationError, match='host unavailable'),
        ):
            try:
                await BenchmarkRunner.run_benchmark_iterations(
                    MagicMock(),
                    [['init', 'workload']],
                    'local',
                    object(),
                    {},
                    {
                        'init_mode': 'fast',
                        'init_entrypoint': 'generator.py',
                        'workload_path': str(WORKLOAD_PROFILES_PATH / 'pagila'),
                        'workload_scale': 0.01,
                    },
                )
            finally:
                reset.assert_not_awaited()

    asyncio.run(scenario())


def test_legacy_run_recovers_interrupted_fast_settings_before_reset():
    from pg_perf_bench.benchmark import BenchmarkRunner

    async def scenario():
        guard = MagicMock(open=AsyncMock(), close=AsyncMock())

        async def reset(*args):
            guard.open.assert_awaited_once_with(recover_only=True)
            guard.close.assert_awaited_once()

        with (
            patch('pg_perf_bench.benchmark.InitializationSettings', return_value=guard) as factory,
            patch.object(BenchmarkRunner, 'reset_db_environment', reset),
            patch.object(
                BenchmarkRunner,
                'run_benchmark_with_evidence',
                AsyncMock(return_value={'metrics': {'tps': 1}}),
            ),
        ):
            factory.recovery_pending.return_value = True
            await BenchmarkRunner.run_benchmark_iterations(
                MagicMock(),
                [['legacy init', 'workload']],
                'local',
                object(),
                {},
                {},
            )
            assert factory.call_args.args[2].fsync == 'keep'

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('changes', 'legacy', 'recoverable'),
    [
        ({'address': '127.0.0.2/32', 'port': 6432}, False, True),
        ({'host': 'replica\n1:2'}, False, False),
        ({'host': 'primary\n1:3'}, False, False),
        ({'data_directory': '/moved-data'}, False, False),
        ({}, True, True),
        ({'address': '127.0.0.2/32'}, True, False),
    ],
)
def test_journal_recovers_same_member_and_rejects_changed_or_ambiguous_host(
    tmp_path, changes, legacy, recoverable
):
    identity = {
        'system_identifier': '123',
        'data_directory': '/data',
        'address': '127.0.0.1/32',
        'port': 5432,
        'host': 'primary\n1:2',
    }
    original = dict(identity)
    if legacy:
        original.pop('host')
    journal = tmp_path / 'old-key.json'
    journal.write_text(json.dumps({'identity': original, 'fsync': 'on', 'auto_value': None}))
    guard = InitializationSettings(
        MagicMock(), {}, LoadOptions(), 'local', None, state_dir=tmp_path
    )
    guard.identity = {**identity, **changes}
    if recoverable:
        path, state = guard._recovery_journal()
        assert path == journal and state['fsync'] == 'on'
    else:
        with pytest.raises(ConfigurationError, match='original database host'):
            guard._recovery_journal()
    assert journal.is_file()


def test_recovery_rejects_sql_failover_when_host_transport_still_targets_old_primary():
    guard = InitializationSettings(
        MagicMock(),
        {},
        LoadOptions(),
        'local',
        MagicMock(run_command=AsyncMock(return_value='old-primary\n1:2\nold postmaster\n')),
    )
    guard.db = MagicMock(
        fetchrow=AsyncMock(
            return_value={
                'system_identifier': '123',
                'data_directory': '/data',
                'address': '127.0.0.1/32',
                'port': 5432,
                'postmaster_pid': 'new postmaster\n',
            }
        )
    )
    with pytest.raises(ConfigurationError, match='target different servers'):
        asyncio.run(guard._identity())
