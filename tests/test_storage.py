import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.executors import ProcessResult
from pg_perf_bench.report.commands import fill_info_report
from pg_perf_bench.storage import build_storage_section, collect_storage_snapshot, vacuum_analyze
from tests.guard_helpers import mock_guard


def test_workload_boundaries_include_vacuum_and_preserve_each_iteration():
    events = []

    async def command(logger, cmd, **kwargs):
        events.append(cmd)
        return ProcessResult(
            argv=(cmd,),
            returncode=0,
            stdout='tps = 10',
            stderr='',
            started_at='',
            elapsed_seconds=1,
        )

    async def vacuum(*args):
        events.append('vacuum')

    async def snapshot(*args):
        events.append('snapshot')
        return {'sequence': len(events)}

    async def reset(*args, **kwargs):
        events.append('reset')

    with (
        patch('pg_perf_bench.benchmark.run_command_result', side_effect=command),
        patch('pg_perf_bench.benchmark.vacuum_analyze', side_effect=vacuum),
        patch('pg_perf_bench.benchmark.collect_storage_snapshot', side_effect=snapshot),
        patch('pg_perf_bench.benchmark.InitializationSettings', side_effect=mock_guard),
        patch.object(BenchmarkRunner, 'reset_db_environment', side_effect=reset),
    ):
        runs = asyncio.run(
            BenchmarkRunner.run_benchmark_iterations(
                MagicMock(),
                [['init1', 'work1'], ['init2', 'work2']],
                'managed',
                None,
                {},
                {'pgbench_iter_name': 'pgbench_clients', 'pgbench_iter_list': [1, 2]},
            )
        )
    assert events == [
        'reset',
        'init1',
        'vacuum',
        'snapshot',
        'work1',
        'snapshot',
        'reset',
        'init2',
        'vacuum',
        'snapshot',
        'work2',
        'snapshot',
    ]
    assert runs[0]['storage'] == {
        'before_workload': {'sequence': 4},
        'after_workload': {'sequence': 6},
    }
    assert runs[1]['storage'] == {
        'before_workload': {'sequence': 10},
        'after_workload': {'sequence': 12},
    }


def test_vacuum_failure_closes_connection_and_does_not_start_workload():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError('vacuum timed out')
    commands = AsyncMock()
    with (
        patch('pg_perf_bench.storage.asyncpg.connect', AsyncMock(return_value=db)),
        patch('pg_perf_bench.benchmark.run_command_result', commands),
        pytest.raises(RuntimeError, match='vacuum timed out'),
    ):
        asyncio.run(
            BenchmarkRunner.run_benchmark_with_evidence(
                MagicMock(),
                ['init', 'work'],
                db_conf={},
                command_timeout=30,
            )
        )
    assert commands.await_count == 1
    db.execute.assert_awaited_once_with('VACUUM ANALYZE', timeout=30)
    db.close.assert_awaited_once()


def test_after_snapshot_waits_for_os_sampler_to_finish():
    events = []

    async def scenario():
        sampler_started = asyncio.Event()
        workload_finished = asyncio.Event()

        async def command(logger, cmd, **kwargs):
            if cmd == 'pgbench -T 1':
                await sampler_started.wait()
                events.append('workload_finished')
                workload_finished.set()
            return ProcessResult(
                argv=(cmd,),
                returncode=0,
                stdout='tps = 10',
                stderr='',
                started_at='',
                elapsed_seconds=1,
            )

        async def sampler(**kwargs):
            events.append('sampler_started')
            sampler_started.set()
            await workload_finished.wait()
            # Provider startup or an explicit sampling duration can extend sampling
            # past pgbench. The final sample must precede the storage queries.
            await asyncio.sleep(0)
            events.append('sampler_finished')
            return {'samples': {'os.cpu': [{'timestamp': 'last sample'}]}}

        async def snapshot(*args):
            events.append('snapshot')
            return {}

        with (
            patch('pg_perf_bench.benchmark.run_command_result', side_effect=command),
            patch('pg_perf_bench.benchmark.vacuum_analyze', AsyncMock()),
            patch('pg_perf_bench.benchmark.collect_storage_snapshot', side_effect=snapshot),
            patch('pg_perf_bench.benchmark.collect_system_metrics', side_effect=sampler),
        ):
            return await BenchmarkRunner.run_benchmark_with_evidence(
                MagicMock(),
                ['init', 'pgbench -T 1'],
                db_conf={},
                command_timeout=30,
                connection_type='local',
                connection=object(),
            )

    result = asyncio.run(scenario())
    assert events == [
        'snapshot',
        'sampler_started',
        'workload_finished',
        'sampler_finished',
        'snapshot',
    ]
    assert result['system_metrics']['samples']['os.cpu'] == [{'timestamp': 'last sample'}]


def test_snapshot_keeps_unavailable_database_and_other_sizes():
    db = AsyncMock()
    db.is_closed = MagicMock(return_value=False)
    db.fetch.side_effect = [
        [
            {'database_oid': 1, 'database_name': 'unavailable', 'is_workload_database': False},
            {'database_oid': 2, 'database_name': 'target', 'is_workload_database': True},
        ],
        [],
        [],
    ]
    db.fetchrow.side_effect = [
        asyncpg_error := RuntimeError('permission denied for database unavailable'),
        {'database_size_bytes': 8192, 'database_size': '8192 bytes'},
    ]
    with patch('pg_perf_bench.storage.asyncpg.connect', AsyncMock(return_value=db)):
        snapshot = asyncio.run(collect_storage_snapshot(MagicMock(), {}))
    item = snapshot['reports']['database_sizes']
    rows = [dict(zip(item['theader'], row, strict=True)) for row in item['data']]
    assert item['collection_status'] == 'partial'
    assert rows[0]['database_name'] == 'target'
    assert rows[0]['database_size_bytes'] == 8192
    assert rows[1]['database_name'] == 'unavailable'
    assert rows[1]['database_size_bytes'] is None
    assert rows[1]['reason'] == str(asyncpg_error)
    assert snapshot['reports']['table_sizes']['collection_status'] == 'empty'
    assert snapshot['reports']['index_sizes']['collection_status'] == 'empty'
    db.close.assert_awaited_once()


def test_captured_sizes_are_not_collected_again_during_final_report_pass():
    with patch('pg_perf_bench.storage.asyncpg.connect', side_effect=RuntimeError('unavailable')):
        snapshot = asyncio.run(collect_storage_snapshot(MagicMock(), {}))
    runs = [
        {
            'iteration': {'index': 1, 'parameter': 'clients', 'value': 4},
            'storage': {'before_workload': snapshot, 'after_workload': deepcopy(snapshot)},
        }
    ]
    section = build_storage_section(runs)
    original = deepcopy(section)
    assert len(section['reports']) == 6
    assert all(item['collection_status'] == 'error' for item in section['reports'].values())
    db = AsyncMock()
    asyncio.run(fill_info_report(MagicMock(), None, db, {}, {'sections': {'storage': section}}))
    db.fetch.assert_not_awaited()
    db.fetchval.assert_not_awaited()
    assert section == original
    assert all('sql_command_file' not in item for item in section['reports'].values())


def test_vacuum_is_run_outside_transaction_with_connect_timeout():
    db = AsyncMock()
    with patch('pg_perf_bench.storage.asyncpg.connect', AsyncMock(return_value=db)) as connect:
        asyncio.run(vacuum_analyze(MagicMock(), {'database': 'target', 'connect_timeout': 2}, 30))
    connect.assert_awaited_once_with(database='target', timeout=2.0)
    db.execute.assert_awaited_once_with('VACUUM ANALYZE', timeout=30)
    db.transaction.assert_not_called()
    db.close.assert_awaited_once()
