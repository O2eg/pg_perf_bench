import asyncio
import base64
import hashlib
import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.cli import _collection_warnings, _runtime_plan, build_parser, execute_namespace
from pg_perf_bench.config import build_runtime_config
from pg_perf_bench.const import ConnectionType
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.executors import ProcessResult
from pg_perf_bench.managed import (
    MANAGED_NO_DATA,
    add_managed_report_metadata,
    read_managed_pg_info,
)
from pg_perf_bench.report.processing import save_report


def managed_arguments(path):
    return [
        'benchmark',
        '--managed-pg-info',
        str(path),
        '--host',
        'managed.example',
        '--port',
        '5432',
        '--user',
        'bench_owner',
        '--database',
        'bench_db',
        '--allow-database-reset',
        '--workload-profile',
        'pagila-htap',
        '--init-fsync',
        'keep',
        '--workload-scale',
        '0.1',
        '--workload-duration-seconds',
        '2',
        '--pgbench-clients',
        '1,2',
    ]


def test_managed_config_needs_no_host_transport_or_server_paths(tmp_path):
    path = tmp_path / 'instance.txt'
    path.write_text('cloud = example\nsize = small\n', encoding='utf-8')
    config = build_runtime_config(build_parser().parse_args(managed_arguments(path)))
    assert config.host.connection_type == ConnectionType.MANAGED
    assert config.host.pg_data_path is None and config.host.pg_bin_path is None
    assert config.host.connection_kwargs(config.database) == {}
    assert config.workload.managed_pg_info == str(path)
    assert config.workload.as_legacy_dict(config.host)['managed_pg_info'] == str(path)
    first = _runtime_plan(config)
    path.write_text('cloud = example\nsize = large\n', encoding='utf-8')
    second = _runtime_plan(config)
    assert first['plan_hash'] != second['plan_hash']
    assert first['inputs']['managed_pg_info']['hash'] != second['inputs']['managed_pg_info']['hash']


@pytest.mark.parametrize(
    'option',
    [
        ['--drop-os-caches'],
        ['--pg-custom-config', '/some/config'],
        ['--connection-type', 'docker'],
        ['--connection-type', 'ssh'],
        ['--container-name', 'container'],
        ['--ssh-host', 'host'],
    ],
)
def test_managed_rejects_incompatible_server_management_options(tmp_path, option):
    path = tmp_path / 'instance'
    path.write_text('instance metadata', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='--managed-pg-info cannot be combined'):
        build_runtime_config(build_parser().parse_args([*managed_arguments(path), *option]))


def test_managed_keeps_database_reset_confirmation_and_protection(tmp_path):
    path = tmp_path / 'instance'
    path.write_bytes(b'metadata')
    args = managed_arguments(path)
    args.remove('--allow-database-reset')
    with pytest.raises(ConfigurationError, match='--allow-database-reset'):
        build_runtime_config(build_parser().parse_args(args))
    args = managed_arguments(path)
    args[args.index('--database') + 1] = 'postgres'
    with pytest.raises(ConfigurationError, match='protected database'):
        build_runtime_config(build_parser().parse_args(args))


@pytest.mark.parametrize(
    'payload',
    [
        b'not JSON {\r\nprovider: example\r\n',
        b'\x00\xff\x80PDF-like-data',
        b'',
    ],
)
def test_metadata_is_lossless_for_any_file_content(tmp_path, payload):
    path = tmp_path / 'instance.anything'
    path.write_bytes(payload)
    info = read_managed_pg_info(path)
    report = {
        'report_name': 'metadata',
        'sections': {'db': {'reports': {}}, 'result': {'reports': {}}},
    }
    add_managed_report_metadata(report, info)
    save_report(MagicMock(), report, tmp_path)
    saved_report = json.loads((tmp_path / 'metadata.json').read_text())
    saved = saved_report['managed_pg_info']
    restored = (
        base64.b64decode(saved['content'])
        if saved['encoding'] == 'base64'
        else saved['content'].encode('utf-8')
    )
    assert restored == payload
    assert saved['hash'] == 'sha256:' + hashlib.sha256(payload).hexdigest()
    assert saved['size_bytes'] == len(payload)
    item = saved_report['sections']['db']['reports']['managed_pg_info']
    assert item['item_type'] == 'plain_text'
    assert item['data'] == saved['content']
    assert ('Base64' in item['description']) == (saved['encoding'] == 'base64')


@pytest.mark.parametrize('directory', [False, True])
def test_missing_or_non_file_metadata_fails_before_execution(tmp_path, directory):
    path = tmp_path / 'metadata'
    if directory:
        path.mkdir()
    with pytest.raises(ConfigurationError, match='existing regular file'):
        build_runtime_config(build_parser().parse_args(managed_arguments(path)))


def test_managed_run_only_uses_database_and_workload_clients(tmp_path):
    path = tmp_path / 'instance.yaml'
    path.write_text('provider: example\nmemory: 16 GiB\n', encoding='utf-8')
    args = build_parser().parse_args(
        [*managed_arguments(path), '--collect-pg-logs', '--init-mode', 'legacy']
    )
    tasks = MagicMock()
    for method in ('check_db_access', 'drop_db', 'init_db', 'check_user_db_access'):
        setattr(tasks, method, AsyncMock())
    db = MagicMock()
    db.close = AsyncMock()
    db.execute = AsyncMock()
    db.fetchval = AsyncMock(return_value='PostgreSQL 18.4')
    db.fetch = AsyncMock(return_value=[])
    connect = AsyncMock(return_value=db)
    pgbench_output = (
        'number of clients: 1\nduration: 2 s\n'
        'number of transactions actually processed: 100\n'
        'latency average = 1.25 ms\ninitial connection time = 2.50 ms\n'
        'tps = 80.75 (without initial connection time)\n'
    )

    async def command_result(_logger, command, **_kwargs):
        return ProcessResult(
            argv=('/bin/bash', '-lc', command),
            returncode=0,
            stdout=pgbench_output,
            stderr='',
            started_at='2026-09-15T10:00:00Z',
            elapsed_seconds=2,
        )

    async def scenario():
        with (
            patch.object(
                BenchmarkRunner, 'setup_connection', side_effect=AssertionError('host transport')
            ),
            patch(
                'pg_perf_bench.benchmark.get_conn_type_tasks',
                side_effect=AssertionError('host lifecycle'),
            ),
            patch('pg_perf_bench.benchmark.DBTasks', return_value=tasks),
            patch('pg_perf_bench.benchmark.run_command_result', side_effect=command_result),
            patch(
                'pg_perf_bench.benchmark.collect_system_metrics',
                side_effect=AssertionError('OS sampler'),
            ),
            patch(
                'pg_perf_bench.benchmark.collect_db_logs', side_effect=AssertionError('server logs')
            ),
            patch(
                'pg_perf_bench.report.commands.run_shell_command',
                side_effect=AssertionError('host facts'),
            ),
            patch('pg_perf_bench.benchmark.asyncpg.connect', connect),
            patch.object(
                BenchmarkRunner,
                'collect_compatibility_evidence',
                AsyncMock(
                    return_value={
                        'server': {'version_num': 180004},
                        'load_generator': {'pgbench': '18.4'},
                    }
                ),
            ),
        ):
            return await execute_namespace(args, MagicMock())

    report = asyncio.run(scenario())
    for method in ('check_db_access', 'drop_db', 'init_db', 'check_user_db_access'):
        assert getattr(tasks, method).await_count == 2
    assert db.fetchval.await_count > 0 and db.fetch.await_count > 0
    assert all(call.kwargs['statement_cache_size'] == 0 for call in connect.await_args_list)
    assert len(report['benchmark_runs']) == 2
    assert db.execute.await_count == 2
    assert len(report['sections']['storage']['reports']) == 12
    assert all('system_metrics' not in run for run in report['benchmark_runs'])
    assert report['invocation']['connection_type'] == 'managed'
    assert report['invocation']['managed_postgresql'] is True
    assert report['invocation']['metrics']['engine'] is None
    assert report['benchmark_methodology']['server_restarted_before_each_iteration'] is False
    assert report['benchmark_methodology']['system_metrics_collected_during_workload'] is False
    assert report['managed_pg_info']['content'] == path.read_text()
    sections = report['sections']
    unavailable = [
        *sections['system']['reports'].values(),
        *sections['os_metrics']['reports'].values(),
        sections['db']['reports']['pg_config'],
        sections['result']['reports']['logs'],
    ]
    assert all(item['data'] == MANAGED_NO_DATA for item in unavailable)
    assert all(item['collection_status'] == 'unsupported' for item in unavailable)
    assert sections['db']['reports']['managed_pg_info']['item_type'] == 'plain_text'
    assert sections['db']['reports']['managed_pg_info']['data'] == path.read_text()
    assert _collection_warnings(report) == []
    changed = deepcopy(report)
    changed['managed_pg_info']['hash'] = 'different-instance'
    assert (
        BenchmarkRunner.environment_evidence(changed)['identity_hash']
        != report['environment_evidence']['identity_hash']
    )
