"""Cancelled task diagnostics must survive the Python 3.10 Task boundary."""

import asyncio
import json
import time
from copy import deepcopy

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.errors import CommandFailure, exception_evidence
from tests.test_interrupted_benchmark import partial_report


@pytest.mark.parametrize('wait_for', [False, True])
def test_cancelled_task_retains_checkpoint_and_partial_report_evidence(tmp_path, wait_for):
    async def scenario():
        reference = partial_report(cancelled=True)
        started = asyncio.Event()
        command = CommandFailure('pgbench', -15, 'partial secret-value', 'cancelled', 1)

        async def child():
            try:
                started.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                exc.failure = command
                exc.benchmark_run = reference['failed_iteration']
                exc.completed_runs = reference['benchmark_runs']
                raise

        task = asyncio.create_task(child())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            if wait_for:
                await asyncio.wait_for(task, timeout=2)
            else:
                await task
        error = caught.value
        assert task.cancelled()  # Evidence handling must not turn cancellation into success.
        assert exception_evidence(error, 'failure') is command
        results = exception_evidence(error, 'completed_runs')
        assert results == reference['benchmark_runs']
        path = tmp_path / 'checkpoint.json'
        BenchmarkRunner.write_checkpoint(
            {'checkpoint_path': str(path)},
            {'password': 'secret-value'},
            results,
            'cancelled',
            error,
        )
        checkpoint = json.loads(path.read_text())
        assert checkpoint['completed_iterations'] == 1
        assert checkpoint['failed_iteration'] == reference['failed_iteration']
        assert checkpoint['failed_command']['returncode'] == -15
        assert 'partial' in checkpoint['failed_command']['stdout']
        assert 'secret-value' not in path.read_text()
        report = BenchmarkRunner.partial_report(
            deepcopy(reference),
            {
                'workload_conf': {
                    'pgbench_iter_name': 'pgbench_clients',
                    'pgbench_iter_list': [8, 16, 64],
                },
                'report_conf': {'report_name': 'cancellation'},
            },
            error,
            started_at='start',
            started_clock=time.monotonic(),
        )
        assert report['benchmark_status'] == 'cancelled'
        assert report['benchmark_runs'] == reference['benchmark_runs']
        assert report['failed_iteration'] == reference['failed_iteration']
        assert report['maximum_tps']['tps'] == 10

    asyncio.run(scenario())


def test_cancellation_lookup_respects_current_value_and_ignores_unrelated_errors():
    inner = asyncio.CancelledError()
    inner.benchmark_run = {'workload': 'original'}
    outer = asyncio.CancelledError()
    outer.__context__ = inner
    assert exception_evidence(outer, 'benchmark_run') == inner.benchmark_run
    outer.benchmark_run = None
    assert exception_evidence(outer, 'benchmark_run', {}) is None
    unrelated = RuntimeError('new operation')
    unrelated.__context__ = inner
    assert exception_evidence(unrelated, 'benchmark_run') is None
    inner.__context__ = outer  # A malformed context cycle must not hang reporting.
    assert exception_evidence(outer, 'missing', 'fallback') == 'fallback'
