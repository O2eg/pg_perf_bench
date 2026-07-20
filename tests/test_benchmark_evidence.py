import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.executors import ProcessResult
from pg_perf_bench.report.commands import benchmark_result, chart_tps


def _result(command, stdout='', stderr='', returncode=0):
    return ProcessResult(
        argv=('/bin/sh', '-c', command),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        started_at='2026-01-01T00:00:00+00:00',
        elapsed_seconds=1.25,
    )


def test_benchmark_preserves_raw_evidence_and_redacts_password():
    password = 'super-secret'
    pgbench_output = """number of clients: 4
duration: 10 s
number of transactions actually processed: 100
latency average = 1.25 ms
initial connection time = 2.50 ms
tps = 80.75 (without initial connection time)
"""
    command_results = AsyncMock(
        side_effect=[
            _result(f'init --password={password}', stdout='initialized'),
            _result(f'run --password={password}', stdout=pgbench_output),
        ]
    )

    async def scenario():
        with patch(
            'pg_perf_bench.benchmark.run_command_result',
            command_results,
        ):
            return await BenchmarkRunner.run_benchmark_with_evidence(
                MagicMock(),
                [f'init --password={password}', f'run --password={password}'],
                db_conf={'password': password},
                command_timeout=30,
            )

    evidence = asyncio.run(scenario())
    assert evidence['metrics']['tps'] == 80.75
    assert evidence['legacy_metrics'] == [4, 10, 100, 1.25, 2.5, 80.75]
    assert password not in str(evidence)
    assert evidence['workload']['stdout'] == pgbench_output
    assert command_results.await_args_list[0].kwargs['env']['PGPASSWORD'] == password


def test_iteration_evidence_identifies_axis_value():
    async def scenario():
        with (
            patch.object(
                BenchmarkRunner,
                'reset_db_environment',
                new=AsyncMock(),
            ),
            patch.object(
                BenchmarkRunner,
                'run_benchmark_with_evidence',
                new=AsyncMock(return_value={'metrics': {'tps': 1.0}}),
            ),
        ):
            return await BenchmarkRunner.run_benchmark_iterations(
                MagicMock(),
                [['init', 'run'], ['init', 'run']],
                'local',
                MagicMock(),
                {'password': None},
                {
                    'pgbench_iter_name': 'pgbench_clients',
                    'pgbench_iter_list': [1, 8],
                    'command_timeout': 30,
                },
            )

    results = asyncio.run(scenario())
    assert results[0]['iteration'] == {
        'index': 1,
        'parameter': 'pgbench_clients',
        'value': 1,
    }
    assert results[1]['iteration']['value'] == 8


def test_unparseable_tps_marks_report_items_partial():
    report_data = {
        'pgbench_outputs': [[1, 10, 100, 1.0, 1.0, None]],
        'workload_conf': {
            'pgbench_iter_name': 'pgbench_clients',
            'pgbench_iter_list': [1],
        },
        'report_conf': {'report_name': 'test'},
    }
    table = {}
    chart = {'data': {}}
    benchmark_result(report_data, table)
    chart_tps(report_data, chart)
    assert table['collection_status'] == 'partial'
    assert chart['collection_status'] == 'partial'


def test_partly_parseable_tps_marks_chart_partial():
    report_data = {
        'pgbench_outputs': [
            [1, 10, 100, 1.0, 1.0, 10.0],
            [2, 10, 100, 1.0, 1.0, None],
        ],
        'workload_conf': {
            'pgbench_iter_name': 'pgbench_clients',
            'pgbench_iter_list': [1, 2],
        },
        'report_conf': {'report_name': 'test'},
    }
    chart = {'data': {}}
    chart_tps(report_data, chart)
    assert chart['collection_status'] == 'partial'
    assert chart['data']['series'][0]['data'] == [[1, 10.0]]
