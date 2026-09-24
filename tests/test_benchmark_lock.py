import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.errors import ConfigurationError
from tests.guard_helpers import mock_guard


@pytest.mark.parametrize('mode', ['managed', 'plain', 'patroni'])
@pytest.mark.parametrize('failure', [None, 'before-drop', 'after-restart'])
def test_reset_checks_lock_and_reacquires_before_create(mode, failure):
    if mode == 'managed' and failure == 'after-restart':
        pytest.skip('Managed resets do not restart the server')
    events = []
    controller, tasks, lifecycle, patroni = mock_guard(), MagicMock(), MagicMock(), MagicMock()

    def record(label, error=False):
        async def call(*args, **kwargs):
            events.append(label)
            if error:
                raise ConfigurationError('server lock unavailable')

        return AsyncMock(side_effect=call)

    controller.assert_held = record('lock-check', failure == 'before-drop')
    controller.reopen_after_restart = record('reacquire', failure == 'after-restart')
    for name in ('check_db_access', 'drop_db', 'init_db', 'check_user_db_access'):
        setattr(tasks, name, record(name))
    for name in ('start_db', 'stop_db', 'sync'):
        setattr(lifecycle, name, record(name))
    patroni.verify_database = record('verify')
    patroni.restart = record('restart')
    config = {'allow_database_reset': True, 'managed': mode == 'managed', 'pg_data_path': '/data'}
    with (
        patch('pg_perf_bench.benchmark.DBTasks', return_value=tasks),
        patch(
            'pg_perf_bench.benchmark.PatroniController.detect',
            AsyncMock(return_value=patroni if mode == 'patroni' else None),
        ),
        patch('pg_perf_bench.benchmark.get_conn_type_tasks', return_value=lambda **kw: lifecycle),
    ):

        async def run():
            await BenchmarkRunner.reset_db_environment(
                MagicMock(), mode, MagicMock(), {}, config, reset_guard=controller
            )

        if failure:
            with pytest.raises(RuntimeError, match='server lock'):
                asyncio.run(run())
            tasks.init_db.assert_not_awaited()
            if failure == 'before-drop':
                tasks.drop_db.assert_not_awaited()
                lifecycle.stop_db.assert_not_awaited()
                patroni.restart.assert_not_awaited()
        else:
            asyncio.run(run())
            assert events.index('lock-check') < events.index('drop_db')
            if mode != 'managed':
                assert events.index('drop_db') < events.index('reacquire') < events.index('init_db')
                assert events.index('restart' if mode == 'patroni' else 'stop_db') < events.index(
                    'reacquire'
                )


def test_lock_conflict_prevents_custom_config_write_and_legacy_initialization():
    controller = mock_guard()
    controller.open.side_effect = ConfigurationError('server lock unavailable')
    client = MagicMock(send_pg_config_file=AsyncMock())
    with (
        patch('pg_perf_bench.benchmark.InitializationSettings', return_value=controller),
        patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()) as reset,
        patch.object(BenchmarkRunner, 'run_benchmark_with_evidence', AsyncMock()) as workload,
    ):
        with pytest.raises(ConfigurationError, match='server lock'):
            asyncio.run(
                BenchmarkRunner.run_benchmark_iterations(
                    MagicMock(),
                    [['init', 'work']],
                    'local',
                    client,
                    {'database': 'bench'},
                    {
                        'init_policy': 'once',
                        'init_mode': 'legacy',
                        'pg_custom_config': '/new.conf',
                        'pg_data_path': '/data',
                    },
                )
            )
    client.send_pg_config_file.assert_not_awaited()
    reset.assert_not_awaited()
    workload.assert_not_awaited()
