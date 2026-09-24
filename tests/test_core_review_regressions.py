import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pg_diag.sampler_runtime import SamplerCollection

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.errors import CommandExecutionError, CommandFailure
from pg_perf_bench.executors import ProcessResult
from pg_perf_bench.report.benchmark_status import build_execution_section
from pg_perf_bench.system_metrics import collect_system_metrics


def sample(number):
    return {'timestamp': f'2026-01-01T00:00:0{number}+00:00', 'rows': []}


def test_slow_disk_does_not_block_subsequent_proc_windows():
    async def scenario():
        stop = asyncio.Event()
        disk_started = asyncio.Event()
        release_disk = asyncio.Event()
        proc_windows = 0

        async def provider(content, host, duration, interval, outputs):
            nonlocal proc_windows
            if outputs == {'os.disk'}:
                disk_started.set()
                await release_disk.wait()
                return SamplerCollection({'os.disk': [sample(1)]}, [])
            assert 'os.disk' not in outputs
            await disk_started.wait()
            proc_windows += 1
            if proc_windows == 3:
                stop.set()
                release_disk.set()
            return SamplerCollection({'os.memory': [sample(proc_windows)]}, [])

        with patch('pg_perf_bench.system_metrics.collect_sampler_providers', provider):
            result = await asyncio.wait_for(
                collect_system_metrics(
                    connection_type='local',
                    connection=object(),
                    duration_seconds=1,
                    interval_seconds=0.1,
                    stop_event=stop,
                ),
                timeout=2,
            )
        assert result['samples']['os.memory'] == [sample(1), sample(2), sample(3)]
        assert result['samples']['os.disk'] == [sample(1)]
        assert result['errors'] == []

    asyncio.run(scenario())


@pytest.mark.parametrize('cancel_count', [1, 2])
def test_cancellation_during_final_window_keeps_prior_and_final_samples(cancel_count):
    async def scenario():
        second_window = asyncio.Event()
        workload_done = asyncio.Event()
        release_window = asyncio.Event()
        proc_windows = 0

        async def provider(content, host, duration, interval, outputs):
            nonlocal proc_windows
            if outputs == {'os.disk'}:
                await release_window.wait()
                return SamplerCollection({'os.disk': [sample(1)]}, [])
            proc_windows += 1
            if proc_windows == 2:
                second_window.set()
                await release_window.wait()
            return SamplerCollection({'os.memory': [sample(proc_windows)]}, [])

        async def command(logger, cmd, **kwargs):
            if cmd != 'init':
                await second_window.wait()
                workload_done.set()
            return ProcessResult(
                argv=(cmd,),
                returncode=0,
                stdout='tps = 10',
                stderr='',
                started_at='2026-01-01T00:00:00+00:00',
                elapsed_seconds=0.1,
            )

        with (
            patch('pg_perf_bench.system_metrics.collect_sampler_providers', provider),
            patch('pg_perf_bench.benchmark.run_command_result', command),
            patch('pg_perf_bench.benchmark.vacuum_analyze', AsyncMock()),
            patch('pg_perf_bench.benchmark.collect_storage_snapshot', AsyncMock(return_value={})),
        ):
            task = asyncio.create_task(
                BenchmarkRunner.run_benchmark_with_evidence(
                    MagicMock(),
                    ['init', 'pgbench -T 1'],
                    db_conf={},
                    command_timeout=5,
                    connection_type='local',
                    connection=object(),
                    system_metrics_interval=0.1,
                )
            )
            try:
                await asyncio.wait_for(workload_done.wait(), timeout=2)
                for _ in range(cancel_count):
                    task.cancel()
                    await asyncio.sleep(0)
                release_window.set()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task, timeout=2)
                evidence = caught.value.benchmark_run
                assert evidence['status'] == 'cancelled'
                assert evidence['system_metrics']['samples']['os.memory'] == [sample(1), sample(2)]
                assert evidence['system_metrics']['samples']['os.disk'] == [sample(1)]
                assert evidence['system_metrics']['errors'] == []
                assert evidence['workload']['stdout'] == 'tps = 10'
                assert 'metrics' not in evidence
            finally:
                release_window.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['failure', 'cancelled', 'success'])
def test_cleanup_failure_preserves_original_error_or_completed_measurement(tmp_path, outcome):
    async def scenario():
        evidence = {
            'init': {'returncode': 0},
            'workload': {'stdout': 'tps = 10', 'returncode': 0},
            'system_metrics': {'samples': {'os.memory': [sample(1)]}},
            'storage': {'before_workload': {}},
            'initialization': {'fsync_after': 'on'},
        }
        primary = (
            asyncio.CancelledError()
            if outcome == 'cancelled'
            else CommandExecutionError(CommandFailure('pgbench', 2, 'partial', 'SQL failure', 3))
        )
        primary.benchmark_run = evidence
        if outcome == 'success':
            evidence.update(status='completed', metrics={'tps': 10})
        work = (
            AsyncMock(return_value=evidence)
            if outcome == 'success'
            else AsyncMock(side_effect=primary)
        )
        cleanup_error = ConnectionError('cleanup failed secret-password')
        guard = MagicMock()
        guard.open = AsyncMock()
        guard.assert_held = AsyncMock()
        guard.replicas = {}
        checkpoint_path = tmp_path / 'progress.json'
        config = {
            'init_mode': 'fast',
            'workload_path': '/tmp',
            'init_entrypoint': 'unused',
            'init_fsync': 'keep',
            'reset_mode': 'schema',
            'pgbench_iter_list': [8, 16],
            'pgbench_iter_name': 'pgbench_clients',
            'checkpoint_path': str(checkpoint_path),
        }
        logger = MagicMock()
        with (
            patch(
                'pg_perf_bench.benchmark.load_plan', return_value=SimpleNamespace(schemas=['app'])
            ),
            patch('pg_perf_bench.benchmark.InitializationSettings', return_value=guard),
            patch('pg_perf_bench.benchmark.check_workload_session', AsyncMock()),
            patch('pg_perf_bench.benchmark.DBTasks.check_schema_reset', AsyncMock()),
            patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()),
            patch.object(BenchmarkRunner, 'run_benchmark_with_evidence', work),
            patch(
                'pg_perf_bench.benchmark.close_initialization_settings',
                AsyncMock(side_effect=cleanup_error),
            ),
        ):
            with pytest.raises(BaseException) as caught:
                await BenchmarkRunner.run_benchmark_iterations(
                    logger,
                    [['init', 'work'], ['init', 'work']],
                    'local',
                    object(),
                    {'database': 'test', 'password': 'secret-password'},
                    config,
                )
        error = caught.value
        work.assert_awaited_once()
        checkpoint = json.loads(checkpoint_path.read_text())
        assert 'secret-password' not in checkpoint_path.read_text()
        assert 'secret-password' not in str(logger.warning.call_args_list)
        failed = checkpoint['failed_iteration']
        assert failed['cleanup_error']['message'] == 'cleanup failed ***'
        assert failed['iteration']['value'] == 8
        section = build_execution_section(
            error.completed_runs, failed_run=failed, error=failed['error']
        )
        assert 'cleanup failed ***' in section['reports']['cleanup_error']['data']
        if outcome == 'success':
            assert error is cleanup_error
            assert checkpoint['completed_iterations'] == 1
            completed = error.completed_runs[0]
            assert completed['metrics']['tps'] == 10
            assert completed['system_metrics'] == evidence['system_metrics']
            assert failed['failure_phase'] == 'cleanup'
            assert 'metrics' not in failed
            assert [row[2] for row in section['reports']['timing']['data']] == [
                'completed',
                'cleanup_failed',
            ]
        else:
            assert error is primary
            assert checkpoint['completed_iterations'] == 0
            assert failed['initialization'] == evidence['initialization']
            assert failed['system_metrics'] == evidence['system_metrics']
            assert failed['storage'] == evidence['storage']
            assert checkpoint['status'] == ('cancelled' if outcome == 'cancelled' else 'failed')

    asyncio.run(scenario())
