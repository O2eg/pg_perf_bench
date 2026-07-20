import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.cli import _legacy_argv, build_parser, execute_namespace, main
from pg_perf_bench.config import build_runtime_config
from pg_perf_bench.errors import ConfigurationError


def _benchmark_arguments(database='bench_db'):
    return [
        'benchmark',
        '--connection-type',
        'local',
        '--pg-host',
        '127.0.0.1',
        '--pg-port',
        '5432',
        '--pg-database',
        database,
        '--pg-data-path',
        '/tmp/pgdata',
        '--pg-bin-path',
        '/usr/lib/postgresql/16/bin',
        '--benchmark-type',
        'default',
        '--pgbench-clients',
        '1,4',
        '--pgbench-path',
        'pgbench',
        '--psql-path',
        'psql',
        '--init-command',
        'pgbench -i ARG_PG_DATABASE',
        '--workload-command',
        'pgbench -c ARG_PGBENCH_CLIENTS ARG_PG_DATABASE',
    ]


def test_legacy_mode_and_machine_options_are_normalized():
    normalized = _legacy_argv(
        [
            '--mode=collect-sys-info',
            '--connection-type=local',
            '--machine',
            '--request-id',
            'request-1',
        ]
    )
    assert normalized[:3] == ['--machine', '--request-id', 'request-1']
    assert normalized[3] == 'collect-sys-info'


def test_collect_db_does_not_require_data_directory():
    args = build_parser().parse_args(
        [
            'collect-db-info',
            '--connection-type',
            'local',
            '--pg-host',
            '127.0.0.1',
            '--pg-port',
            '5432',
            '--pg-database',
            'postgres',
            '--pg-bin-path',
            '/usr/lib/postgresql/16/bin',
        ]
    )
    config = build_runtime_config(args)
    assert config.host.pg_data_path is None
    assert config.database.database == 'postgres'


def test_benchmark_requires_explicit_database_reset_confirmation():
    args = build_parser().parse_args(_benchmark_arguments())
    with pytest.raises(ConfigurationError, match='allow-database-reset'):
        build_runtime_config(args)


def test_benchmark_refuses_protected_database_even_when_confirmed():
    args = build_parser().parse_args([*_benchmark_arguments('postgres'), '--allow-database-reset'])
    with pytest.raises(ConfigurationError, match='protected database'):
        build_runtime_config(args)


def test_machine_capabilities_is_one_json_document(capsys):
    assert main(['--machine', 'capabilities']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'succeeded'
    assert output['result']['safety']['os_cache_drop_is_opt_in'] is True


def test_validate_command_succeeds(capsys):
    assert main(['validate']) == 0
    assert capsys.readouterr().out.strip() == 'OK'


def test_subcommand_mode_is_forwarded_to_collection_backend():
    args = build_parser().parse_args(['collect-sys-info', '--connection-type', 'local'])
    collect = AsyncMock(return_value={'report_name': 'test', 'sections': {}})
    with patch('pg_perf_bench.cli.InfoCollector.collect_info', new=collect):
        asyncio.run(execute_namespace(args, MagicMock()))
    assert collect.await_args.kwargs['args']['mode'] == 'collect-sys-info'


def test_machine_plan_uses_machine_envelope(capsys):
    assert (
        main(
            [
                '--machine',
                'plan',
                'collect-sys-info',
                '--connection-type',
                'local',
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'succeeded'
    assert output['result']['schema_version'] == 'pg_perf_bench/plan-v1'


def test_machine_parse_failure_uses_machine_envelope(capsys):
    assert main(['--machine', 'benchmark']) == 2
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output['status'] == 'failed'
    assert output['error']['code'] == 'validation_error'
    assert captured.err == ''


def test_unsafe_report_name_fails_before_execution():
    args = build_parser().parse_args(
        ['collect-sys-info', '--connection-type', 'local', '--report-name', '../bad']
    )
    with pytest.raises(ConfigurationError, match='unsafe --report-name'):
        build_runtime_config(args)
