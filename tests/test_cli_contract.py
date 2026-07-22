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
        '--host',
        '127.0.0.1',
        '--port',
        '5432',
        '--database',
        database,
        '--pg-data-path',
        '/tmp/pgdata',
        '--pg-bin-path',
        '/usr/lib/postgresql/18/bin',
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
            '--host',
            '127.0.0.1',
            '--port',
            '5432',
            '--database',
            'postgres',
            '--pg-bin-path',
            '/usr/lib/postgresql/18/bin',
        ]
    )
    config = build_runtime_config(args)
    assert config.host.pg_data_path is None
    assert config.database.database == 'postgres'


def test_database_and_output_options_follow_pg_diag_cli_naming(tmp_path):
    args = build_parser().parse_args(
        [
            'collect-db-info',
            '--connection-type=local',
            '--host=127.0.0.1',
            '--port=5432',
            '--user=postgres',
            '--database=postgres',
            '--password=secret',
            '--pg-bin-path=/usr/lib/postgresql/18/bin',
            f'--out={tmp_path}',
        ]
    )
    config = build_runtime_config(args)

    assert config.database.host == '127.0.0.1'
    assert config.database.port == 5432
    assert config.database.user == 'postgres'
    assert config.database.database == 'postgres'
    assert config.database.password == 'secret'
    assert config.report_dir == tmp_path


def test_join_accepts_an_exact_repeated_report_list():
    args = build_parser().parse_args(
        [
            'join',
            '--join-task=optimize-db-config',
            '--report=baseline.json',
            '--report=tuned.json',
        ]
    )

    assert args.input_dir is None
    assert args.reports == ['baseline.json', 'tuned.json']


def test_benchmark_requires_explicit_database_reset_confirmation():
    args = build_parser().parse_args(_benchmark_arguments())
    with pytest.raises(ConfigurationError, match='allow-database-reset'):
        build_runtime_config(args)


def test_benchmark_refuses_protected_database_even_when_confirmed():
    args = build_parser().parse_args([*_benchmark_arguments('postgres'), '--allow-database-reset'])
    with pytest.raises(ConfigurationError, match='protected database'):
        build_runtime_config(args)


def test_machine_capabilities_is_one_json_document(capsys):
    assert main(['--machine', '--component-capabilities']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'succeeded'
    assert output['result']['capability_schema_version'] == 'pg_play/capabilities/v1'
    assert (
        output['result']['machine_interface']['capabilities_option'] == '--component-capabilities'
    )
    assert output['result']['commands']['benchmark']['accepts_plan_hash'] is True
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


def test_machine_benchmark_requires_reviewed_plan_hash(capsys):
    assert main(['--machine', *_benchmark_arguments(), '--allow-database-reset']) == 3
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'blocked'
    assert output['error']['code'] == 'precondition_failed'


def test_plan_hash_changes_with_custom_workload_content(tmp_path, capsys):
    workload = tmp_path / 'workload'
    workload.mkdir()
    script = workload / 'load.sql'
    script.write_text('select 1;\n', encoding='utf-8')
    arguments = [
        *_benchmark_arguments(),
        '--allow-database-reset',
        '--benchmark-type',
        'custom',
        '--workload-path',
        str(workload),
    ]

    assert main(['--machine', 'plan', *arguments]) == 0
    first = json.loads(capsys.readouterr().out)['result']['plan_hash']
    script.write_text('select 2;\n', encoding='utf-8')
    assert main(['--machine', 'plan', *arguments]) == 0
    second = json.loads(capsys.readouterr().out)['result']['plan_hash']

    assert first != second


def test_plan_hash_does_not_depend_on_runtime_password(monkeypatch, capsys):
    arguments = [*_benchmark_arguments(), '--allow-database-reset']
    monkeypatch.setenv('PGPASSWORD', 'first-runtime-secret')
    assert main(['--machine', 'plan', *arguments]) == 0
    first = json.loads(capsys.readouterr().out)['result']['plan_hash']

    monkeypatch.setenv('PGPASSWORD', 'rotated-runtime-secret')
    assert main(['--machine', 'plan', *arguments]) == 0
    second = json.loads(capsys.readouterr().out)['result']['plan_hash']

    assert first == second


def test_machine_artifact_validation_and_summary(tmp_path, capsys):
    artifact = tmp_path / 'report.json'
    artifact.write_text(
        json.dumps(
            {
                'artifact_schema_version': 'pg_perf_bench/report-v1',
                'report_name': 'bench',
                'sections': {},
                'benchmark_runs': [
                    {
                        'iteration': {'parameter': 'pgbench_clients', 'value': 1},
                        'metrics': {'tps': 42.5},
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    assert main(['--machine', 'summarize', str(artifact)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['result']['benchmark_run_count'] == 1
    assert output['result']['iteration_values'] == [1]
    assert output['result']['tps_values'] == [42.5]
    assert output['result']['maximum_tps']['tps'] == 42.5
    assert output['artifacts'][0]['hash'].startswith('sha256:')


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
