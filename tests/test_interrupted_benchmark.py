import asyncio
import json
import sys
import time
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pg_diag.sampler_runtime import SamplerCollection

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.cli import main
from pg_perf_bench.const import BENCHMARK_TEMPLATE_JSON_PATH
from pg_perf_bench.errors import CommandExecutionError, CommandTimeoutError, exception_evidence
from pg_perf_bench.executors import ProcessResult, run_local_process
from pg_perf_bench.report.processing import get_report_structure
from pg_perf_bench.system_metrics import collect_system_metrics
from tests.guard_helpers import mock_guard


def command_result(command, code=0):
    return ProcessResult(
        argv=(command,),
        returncode=code,
        started_at='2026-01-01T00:00:00+00:00',
        elapsed_seconds=7,
        stdout='number of clients: 8\nduration: 1 s\ntps = 999.0\n',
        stderr='canceling statement due to statement timeout' if code else '',
    )


def test_sampling_continues_beyond_requested_time_and_retains_final_window():
    async def scenario():
        stop = asyncio.Event()
        count = 0

        async def provider(*args):
            nonlocal count
            if args[-1] == {'os.disk'}:
                await stop.wait()
                return SamplerCollection({'os.disk': [{'timestamp': 'disk', 'rows': []}]}, [])
            await asyncio.sleep(0.015)
            count += 1
            if count == 3:
                stop.set()
            return SamplerCollection({'os.memory': [{'timestamp': str(count), 'rows': []}]}, [])

        with patch('pg_perf_bench.system_metrics.collect_sampler_providers', provider):
            result = await collect_system_metrics(
                connection_type='local',
                connection=object(),
                duration_seconds=0.01,
                interval_seconds=0.015,
                stop_event=stop,
            )
        assert len(result['samples']['os.memory']) == 3
        assert result['duration_seconds'] > result['requested_duration_seconds']
        assert result['stop_reason'] == 'workload_finished'

    asyncio.run(scenario())


def test_explicit_sampling_cap_is_honoured_with_running_workload():
    async def scenario():
        stop = asyncio.Event()

        async def provider(*args):
            await asyncio.sleep(args[2])
            return SamplerCollection(
                {name: [{'timestamp': 'sample', 'rows': []}] for name in args[-1]}, []
            )

        with patch('pg_perf_bench.system_metrics.collect_sampler_providers', provider):
            result = await collect_system_metrics(
                connection_type='local',
                connection=object(),
                duration_seconds=1,
                interval_seconds=0.01,
                duration_limit_seconds=0.02,
                stop_event=stop,
            )
        assert result['samples']['os.memory']
        assert result['stop_reason'] == 'duration'
        assert not stop.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['error', 'cancelled', 'timeout'])
def test_failed_iteration_keeps_init_storage_and_os_evidence(tmp_path, mode):
    async def scenario():
        started = asyncio.Event()
        snapshot = {'reports': {}, 'started_at': 'before', 'completed_at': 'before'}

        async def command(logger, cmd, **kwargs):
            if cmd == 'init':
                return command_result(cmd)
            await started.wait()
            if mode == 'cancelled':
                raise asyncio.CancelledError()
            if mode == 'timeout':
                raise CommandTimeoutError('deadline reached')
            return command_result(cmd, 2)

        async def sampler(**kwargs):
            started.set()
            await kwargs['stop_event'].wait()
            return {'samples': {'os.cpu': [{'timestamp': 'last sample'}]}, 'errors': []}

        with (
            patch('pg_perf_bench.benchmark.InitializationSettings', side_effect=mock_guard),
            patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()),
            patch('pg_perf_bench.benchmark.run_command_result', command),
            patch('pg_perf_bench.benchmark.vacuum_analyze', AsyncMock()),
            patch(
                'pg_perf_bench.benchmark.collect_storage_snapshot', AsyncMock(return_value=snapshot)
            ),
            patch('pg_perf_bench.benchmark.collect_system_metrics', sampler),
        ):
            with pytest.raises(
                (CommandExecutionError, CommandTimeoutError, asyncio.CancelledError)
            ):
                await BenchmarkRunner.run_benchmark_iterations(
                    MagicMock(),
                    [['init', 'pgbench -T 1']],
                    'local',
                    object(),
                    {},
                    {
                        'pgbench_iter_list': [8],
                        'pgbench_iter_name': 'pgbench_clients',
                        'checkpoint_path': str(tmp_path / 'run.progress.json'),
                    },
                )
        checkpoint = json.loads((tmp_path / 'run.progress.json').read_text())
        failed = checkpoint['failed_iteration']
        assert checkpoint['completed_iterations'] == 0
        assert failed['iteration']['value'] == 8
        assert failed['init']['returncode'] == 0
        assert failed['storage']['before_workload'] == snapshot
        assert failed['system_metrics']['samples']['os.cpu'][0]['timestamp'] == 'last sample'
        assert 'metrics' not in failed
        assert 'after_workload' not in failed['storage']
        if mode == 'error':
            assert '999.0' in failed['workload']['stdout']
            assert checkpoint['statement_timeout_messages'] == 1

    asyncio.run(scenario())


def partial_report(*, completed=True, cancelled=False):
    template = get_report_structure(BENCHMARK_TEMPLATE_JSON_PATH)
    template['report_name'] = 'partial-test'
    successful = {
        'status': 'completed',
        'iteration': {'index': 1, 'parameter': 'pgbench_clients', 'value': 8},
        'metrics': {'tps': 10.0, 'duration_seconds': 1},
        'legacy_metrics': [8, 1, 10, 1, 0, 10.0],
        'workload': {'elapsed_seconds': 1.5},
    }
    failed = {
        'status': 'failed',
        'iteration': {'index': 2, 'parameter': 'pgbench_clients', 'value': 16},
        'workload': {'stdout': 'tps = 999.0', 'stderr': 'quote"secret', 'elapsed_seconds': 3.0},
        'system_metrics': {
            'samples': {'os.cpu': [{'timestamp': 'sample', 'rows': []}]},
            'charts': {},
        },
    }
    error = asyncio.CancelledError() if cancelled else RuntimeError('failed quote"secret')
    error.completed_runs = [successful] if completed else []
    error.benchmark_run = failed
    report = BenchmarkRunner.partial_report(
        template,
        {
            'workload_conf': {
                'pgbench_iter_list': [8, 16, 64],
                'pgbench_iter_name': 'pgbench_clients',
            },
            'report_conf': {'report_name': 'partial-test'},
        },
        error,
        started_at='start',
        started_clock=time.monotonic(),
        secrets=('quote"secret',),
    )
    return report


@pytest.mark.parametrize('completed', [True, False])
def test_partial_report_never_plots_aborted_tps_and_handles_no_completed_points(completed):
    report = partial_report(completed=completed)
    assert report['benchmark_status'] == 'failed'
    assert len(report['benchmark_runs']) == int(completed)
    assert report['maximum_tps'] == (
        {
            'tps': 10.0,
            'iteration': {'index': 1, 'parameter': 'pgbench_clients', 'value': 8},
            'metrics': {'tps': 10.0, 'duration_seconds': 1},
        }
        if completed
        else None
    )
    points = report['sections']['result']['reports']['chart']['data']['series'][0]['data']
    assert points == ([[8, 10.0]] if completed else [])
    assert report['failed_iteration']['workload']['stdout'] == 'tps = 999.0'
    assert 'secret' not in json.dumps(report)
    assert report['sections']['execution']['reports']['failure']['collection_status'] == 'error'


@pytest.mark.parametrize('cancelled,exit_code', [(False, 6), (True, 7)])
def test_cli_saves_partial_html_json_and_machine_error(tmp_path, capsys, cancelled, exit_code):
    report = partial_report(cancelled=cancelled)
    # Exercise common artifact persistence/machine contract without any database calls.
    with patch('pg_perf_bench.cli.execute_namespace', AsyncMock(return_value=deepcopy(report))):
        code = main(
            [
                '--machine',
                'collect-sys-info',
                '--connection-type',
                'local',
                '--out',
                str(tmp_path),
                '--log-dir',
                str(tmp_path / 'log'),
            ]
        )
    assert code == exit_code
    envelope = json.loads(capsys.readouterr().out)
    assert envelope['status'] == ('cancelled' if cancelled else 'failed')
    assert len(envelope['artifacts']) == 2
    artifact = json.loads((tmp_path / 'partial-test.json').read_text())
    assert artifact['collection_summary']['status'] == 'partial'
    html = (tmp_path / 'partial-test.html').read_text()
    assert 'Benchmark did not complete' in html
    assert 'secret' not in html


def test_process_deadline_keeps_partial_stdout_and_stderr():
    async def scenario():
        with pytest.raises(CommandTimeoutError) as caught:
            await run_local_process(
                [
                    sys.executable,
                    '-u',
                    '-c',
                    'import sys,time; print("progress"); '
                    'print("secret", file=sys.stderr); time.sleep(5)',
                ],
                timeout=0.15,
                secrets=('secret',),
            )
        assert caught.value.failure.stdout == 'progress\n'
        assert caught.value.failure.stderr == '***\n'
        assert caught.value.failure.returncode != 0

    asyncio.run(scenario())


def test_complete_runner_returns_partial_report_without_post_failure_database_queries(tmp_path):
    async def scenario():
        calls = 0

        async def command(logger, cmd, **kwargs):
            nonlocal calls
            if cmd == 'work':
                calls += 1
            return command_result(cmd, 2 if cmd == 'work' and calls == 2 else 0)

        with (
            patch.object(
                BenchmarkRunner,
                'load_iterations_config',
                return_value=[['init', 'work'], ['init', 'work']],
            ),
            patch.object(
                BenchmarkRunner, 'collect_compatibility_evidence', AsyncMock(return_value={})
            ),
            patch('pg_perf_bench.benchmark.InitializationSettings', side_effect=mock_guard),
            patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()),
            patch.object(BenchmarkRunner, 'collect_monitoring_metrics', AsyncMock()) as monitoring,
            patch('pg_perf_bench.benchmark.run_command_result', command),
            patch('pg_perf_bench.benchmark.vacuum_analyze', AsyncMock()),
            patch(
                'pg_perf_bench.benchmark.collect_storage_snapshot',
                AsyncMock(
                    return_value={
                        'reports': {},
                        'started_at': 'before',
                        'completed_at': 'after',
                    }
                ),
            ),
        ):
            report = await BenchmarkRunner.run_benchmark_and_collect_metrics(
                {},
                'managed',
                {},
                {},
                {
                    'managed': True,
                    'pgbench_iter_list': [8, 16],
                    'pgbench_iter_name': 'pgbench_clients',
                    'checkpoint_path': str(tmp_path / 'complete-run.progress.json'),
                },
                {'report_name': 'complete-run'},
                {},
                MagicMock(),
            )
        monitoring.assert_not_awaited()
        assert report['benchmark_status'] == 'failed'
        assert len(report['benchmark_runs']) == 1
        assert report['failed_iteration']['iteration']['value'] == 16
        assert report['invocation']['workload']['iteration_values'] == [8, 16]
        assert report['sections']['result']['reports']['chart']['data']['series'][0]['data'] == [
            [8, 999.0]
        ]

    asyncio.run(scenario())


def test_fast_initialization_evidence_survives_workload_failure():
    init = {
        'phases': [{'name': 'data', 'elapsed_seconds': 10}],
        'fsync_after': 'on',
        'replication_barrier': {'replicas': 2},
    }

    async def scenario():
        with (
            patch('pg_perf_bench.benchmark.workload_environment', return_value={}),
            patch('pg_perf_bench.benchmark.initialize_database', AsyncMock(return_value=init)),
            patch('pg_perf_bench.benchmark.collect_storage_snapshot', AsyncMock(return_value={})),
            patch(
                'pg_perf_bench.benchmark.run_command_result',
                AsyncMock(return_value=command_result('work', 2)),
            ),
        ):
            with pytest.raises(CommandExecutionError) as caught:
                await BenchmarkRunner.run_benchmark_with_evidence(
                    MagicMock(),
                    ['init', 'work'],
                    db_conf={},
                    command_timeout=1,
                    initialization_plan=MagicMock(),
                )
        assert exception_evidence(caught.value, 'benchmark_run')['initialization'] == init
        assert exception_evidence(caught.value, 'benchmark_run')['init']['returncode'] == 0

    asyncio.run(scenario())


def test_sampler_failure_keeps_previous_windows():
    async def scenario():
        with patch(
            'pg_perf_bench.system_metrics.collect_sampler_providers',
            AsyncMock(
                side_effect=[
                    SamplerCollection({'os.memory': [{'timestamp': 'sample', 'rows': []}]}, []),
                    RuntimeError('connection lost'),
                ]
            ),
        ):
            result = await collect_system_metrics(
                connection_type='local',
                connection=object(),
                duration_seconds=1,
                interval_seconds=0.01,
                stop_event=asyncio.Event(),
            )
        assert result['samples']['os.memory'][0]['timestamp'] == 'sample'
        assert result['stop_reason'] == 'collector_error'
        assert result['errors'][0]['message'] == 'connection lost'

    asyncio.run(scenario())


@pytest.mark.parametrize('cancelled', [False, True])
def test_real_local_process_and_os_sampler_preserve_tail_without_database(cancelled):
    async def scenario():
        command_started = asyncio.Event()

        async def command(logger, cmd, **kwargs):
            if cmd == 'init':
                return command_result(cmd)
            command_started.set()
            return await run_local_process(
                [sys.executable, '-u', '-c', 'import time; print("tps = 1.0"); time.sleep(0.3)'],
                timeout=3,
            )

        with (
            patch('pg_perf_bench.benchmark.run_command_result', command),
            patch('pg_perf_bench.benchmark.vacuum_analyze', AsyncMock()),
            patch('pg_perf_bench.benchmark.collect_storage_snapshot', AsyncMock(return_value={})),
        ):
            task = asyncio.create_task(
                BenchmarkRunner.run_benchmark_with_evidence(
                    MagicMock(),
                    ['init', 'pgbench -T 0.05'],
                    db_conf={},
                    command_timeout=3,
                    connection_type='local',
                    connection=object(),
                    system_metrics_interval=0.05,
                )
            )
            await command_started.wait()
            if cancelled:
                await asyncio.sleep(0.15)
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
                result = exception_evidence(caught.value, 'benchmark_run')
                assert 'tps = 1.0' in result['workload']['stdout']
                assert 'metrics' not in result
            else:
                result = await task
                assert result['metrics']['tps'] == 1
            metrics = result['system_metrics']
            assert metrics['duration_seconds'] > metrics['requested_duration_seconds']
            assert metrics['samples']['os.memory']
            assert metrics['samples']['os.cpu']
            assert metrics['stop_reason'] == 'workload_finished'
            assert metrics['finished_at'] >= result['workload_finished_at']

    asyncio.run(scenario())
