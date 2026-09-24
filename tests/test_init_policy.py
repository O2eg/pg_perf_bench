import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.cli import build_parser
from pg_perf_bench.config import build_runtime_config
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.executors.process import ProcessResult
from pg_perf_bench.init_policy import validate_existing_dataset
from pg_perf_bench.initialization import LoadPlan
from pg_perf_bench.report.benchmark_status import build_execution_section
from pg_perf_bench.workloads import build_workload_evidence


def config_args(policy=None, authorize=True):
    args = [
        'benchmark',
        '--managed',
        '--host',
        'db.example',
        '--port',
        '5432',
        '--database',
        'bench',
        '--workload-profile',
        'imdb',
        '--pgbench-clients',
        '8,16,64',
        '--init-fsync',
        'keep',
    ]
    if policy:
        args += ['--init-policy', policy]
    if authorize:
        args += ['--allow-database-reset']
    return args


def runtime(args):
    client = SimpleNamespace(path='/usr/lib/postgresql/18/bin/pgbench')
    with patch(
        'pg_perf_bench.config.select_local_clients',
        return_value=(client, SimpleNamespace(path='/usr/lib/postgresql/18/bin/psql')),
    ):
        return build_runtime_config(build_parser().parse_args(args))


@pytest.mark.parametrize('policy', [None, 'each-iteration', 'once', 'skip'])
def test_cli_policy_defaults_and_legacy_mapping(policy):
    conf = runtime(config_args(policy, authorize=policy != 'skip'))
    assert conf.workload.init_policy == (policy or 'each-iteration')
    assert conf.workload.as_legacy_dict(conf.host)['init_policy'] == (policy or 'each-iteration')
    assert conf.workload.allow_database_reset == (policy != 'skip')


@pytest.mark.parametrize('policy', ['each-iteration', 'once'])
def test_resetting_policies_still_require_authorization(policy):
    with pytest.raises(ConfigurationError, match='allow-database-reset'):
        runtime(config_args(policy, authorize=False))


def test_managed_skip_does_not_require_loader_fsync_options():
    args = config_args('skip', False)
    del args[args.index('--init-fsync') : args.index('--init-fsync') + 2]
    assert runtime(args).workload.init_policy == 'skip'


@pytest.mark.parametrize(
    'option,value', [('--init-command', 'echo init'), ('--drop-os-caches', None)]
)
def test_skip_rejects_inapplicable_options(option, value):
    args = config_args('skip', False) + [option] + ([value] if value else [])
    with pytest.raises(ConfigurationError, match='cannot be combined'):
        runtime(args)


@pytest.mark.parametrize('policy,count', [('each-iteration', 3), ('once', 1), ('skip', 0)])
@pytest.mark.parametrize('mode', ['schema', 'database', 'legacy'])
def test_iteration_reset_load_counts_and_lock_lifetime(policy, count, mode):
    async def scenario():
        plan = LoadPlan(('app',), 'CREATE SCHEMA app', (), ())
        guards = []
        events = []

        def new_guard(*args, **kwargs):
            guard = MagicMock()
            guard.open = AsyncMock()
            guard.assert_held = AsyncMock()
            guard.replicas = {}
            guards.append(guard)
            return guard

        async def close(guard):
            events.append(('close', guards.index(guard)))

        async def work(*args, **kwargs):
            events.append(('work', kwargs['initialize']))
            return {'status': 'completed', 'metrics': {'tps': 10}}

        conf = {
            'init_policy': policy,
            'init_mode': 'legacy' if mode == 'legacy' else 'fast',
            'init_fsync': 'keep',
            'reset_mode': 'schema' if mode == 'schema' else 'database',
            'workload_path': '/tmp',
            'init_entrypoint': 'generator.py',
            'pgbench_iter_list': [8, 16, 64],
            'pgbench_iter_name': 'pgbench_clients',
        }
        with ExitStack() as stack:
            stack.enter_context(patch('pg_perf_bench.benchmark.load_plan', return_value=plan))
            klass = stack.enter_context(
                patch('pg_perf_bench.benchmark.InitializationSettings', side_effect=new_guard)
            )
            klass.recovery_pending.return_value = False
            stack.enter_context(
                patch('pg_perf_bench.benchmark.close_initialization_settings', close)
            )
            stack.enter_context(
                patch('pg_perf_bench.benchmark.check_workload_session', AsyncMock())
            )
            stack.enter_context(
                patch('pg_perf_bench.benchmark.DBTasks.check_schema_reset', AsyncMock())
            )
            validation = stack.enter_context(
                patch(
                    'pg_perf_bench.benchmark.validate_existing_dataset',
                    AsyncMock(return_value={'checked': True}),
                )
            )
            reset = stack.enter_context(
                patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock())
            )
            stack.enter_context(patch.object(BenchmarkRunner, 'run_benchmark_with_evidence', work))
            result = await BenchmarkRunner.run_benchmark_iterations(
                MagicMock(), [['init', 'work']] * 3, 'managed', None, {'database': 'bench'}, conf
            )
        assert reset.await_count == count
        assert sum(value for kind, value in events if kind == 'work') == count
        assert len(result) == 3
        assert validation.await_count == (1 if policy == 'skip' else 0)
        if policy != 'each-iteration':
            # The target-database guard remains held across all workload calls.
            assert events[-1] == ('close', len(guards) - 1)
            assert events.count(events[-1]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize('fast', [False, True])
def test_skipped_initialization_never_executes_loader_or_vacuum(fast):
    async def scenario():
        command = AsyncMock(
            return_value=ProcessResult(
                argv=('work',),
                returncode=0,
                stdout='tps = 10',
                stderr='',
                started_at='2026-01-01T00:00:00+00:00',
                elapsed_seconds=1,
            )
        )
        with (
            patch('pg_perf_bench.benchmark.run_command_result', command),
            patch('pg_perf_bench.benchmark.initialize_database', AsyncMock()) as loader,
            patch('pg_perf_bench.benchmark.vacuum_analyze', AsyncMock()) as vacuum,
            patch('pg_perf_bench.benchmark.collect_storage_snapshot', AsyncMock(return_value={})),
        ):
            result = await BenchmarkRunner.run_benchmark_with_evidence(
                MagicMock(),
                ['init', 'work'],
                db_conf={},
                command_timeout=10,
                initialize=False,
                init_policy='skip',
                initialization_plan=LoadPlan(('app',), 'ddl', (), ()) if fast else None,
            )
        loader.assert_not_awaited()
        vacuum.assert_not_awaited()
        command.assert_awaited_once()
        assert command.call_args.args[1] == 'work'
        assert result['init']['status'] == 'skipped'
        assert result['metrics']['tps'] == 10

    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_once_releases_retained_lock_on_later_failure(cancel):
    async def scenario():
        guard = MagicMock(open=AsyncMock(), assert_held=AsyncMock(), replicas={})
        error = asyncio.CancelledError() if cancel else RuntimeError('work failed')
        with (
            patch('pg_perf_bench.benchmark.InitializationSettings', return_value=guard) as cls,
            patch('pg_perf_bench.benchmark.close_initialization_settings', AsyncMock()) as close,
            patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()),
            patch.object(
                BenchmarkRunner,
                'run_benchmark_with_evidence',
                AsyncMock(side_effect=[{'metrics': {'tps': 10}}, error]),
            ),
        ):
            cls.recovery_pending.return_value = False
            with pytest.raises(type(error)) as caught:
                await BenchmarkRunner.run_benchmark_iterations(
                    MagicMock(),
                    [['i', 'w']] * 3,
                    'managed',
                    None,
                    {'database': 'bench'},
                    {'init_policy': 'once'},
                )
        close.assert_awaited_once_with(guard)
        assert caught.value.benchmark_run['iteration']['index'] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['missing', 'empty', 'invalid'])
def test_skip_rejects_incomplete_existing_schema(case):
    async def scenario():
        db = MagicMock(fetchval=AsyncMock(return_value=case != 'missing'))
        db.fetch = AsyncMock(
            side_effect=[[]]
            if case == 'empty'
            else [[{'relname': 'data'}], [], [{'relname': 'broken'}]]
        )
        with pytest.raises(ConfigurationError):
            await validate_existing_dataset(db, LoadPlan(('app',), 'ddl', (), ()))

    asyncio.run(scenario())


def test_policy_changes_execution_hash_and_records_skipped_commands():
    results = {}
    for policy in ['each-iteration', 'once', 'skip']:
        c = runtime(config_args(policy))
        conf = c.workload.as_legacy_dict(c.host)
        commands = BenchmarkRunner.load_iterations_config(c.database.as_legacy_dict(), conf)
        results[policy] = build_workload_evidence(conf, commands)
    assert len({r['execution_hash'] for r in results.values()}) == 3
    assert results['once']['pgbench']['resolved_commands'][1]['init'] is None
    assert results['skip']['pgbench']['resolved_commands'][0]['init'] is None
    section = build_execution_section([{'init_policy': 'once', 'initialization_performed': False}])
    timing = section['reports']['timing']
    assert timing['data'][0][timing['theader'].index('Initialized this iteration')] is False


def test_skip_builtin_without_init_command():
    args = config_args('skip', False)
    pos = args.index('--workload-profile')
    args[pos : pos + 2] = [
        '--benchmark-type',
        'default',
        '--workload-command',
        'pgbench -c ARG_PGBENCH_CLIENTS ARG_PG_DATABASE',
    ]
    conf = runtime(args)
    assert conf.workload.init_command == ''
    assert conf.workload.init_mode == 'legacy'


def test_skipped_initialization_section_has_explicit_explanation():
    from pg_perf_bench.initialization import build_initialization_section

    report = build_initialization_section(
        [
            {
                'iteration': {'index': 2},
                'init_policy': 'once',
                'initialization_performed': False,
            }
        ],
        {'policy': 'once'},
    )
    assert 'iteration 1' in report['reports']['iteration_2_reuse']['data']


def test_checkpoint_failure_releases_retained_lock_without_masking_error():
    async def scenario():
        guard = MagicMock(open=AsyncMock(), assert_held=AsyncMock(), replicas={})
        original = OSError('cannot write progress')
        with (
            patch('pg_perf_bench.benchmark.InitializationSettings', return_value=guard) as cls,
            patch(
                'pg_perf_bench.benchmark.close_initialization_settings',
                AsyncMock(side_effect=RuntimeError('cleanup failed')),
            ) as close,
            patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()),
            patch.object(
                BenchmarkRunner,
                'run_benchmark_with_evidence',
                AsyncMock(return_value={'metrics': {'tps': 10}}),
            ),
            patch.object(BenchmarkRunner, 'write_checkpoint', side_effect=original),
        ):
            cls.recovery_pending.return_value = False
            with pytest.raises(OSError, match='cannot write progress') as caught:
                await BenchmarkRunner._run_benchmark_iterations(
                    MagicMock(),
                    [['i', 'w']] * 2,
                    'managed',
                    None,
                    {'database': 'bench'},
                    {'init_policy': 'once'},
                    [],
                )
        close.assert_awaited_once_with(guard)
        assert caught.value.benchmark_run['cleanup_error']['message'] == 'cleanup failed'

    asyncio.run(scenario())
