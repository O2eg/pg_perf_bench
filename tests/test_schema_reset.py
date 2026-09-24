import asyncio
import shlex
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.cli import _runtime_plan, build_parser
from pg_perf_bench.config import build_runtime_config, pgbench_protocol
from pg_perf_bench.const import ConnectionType
from pg_perf_bench.db_operations.db import validate_reset_schemas
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import LoadOptions
from pg_perf_bench.initialization_settings import InitializationSettings
from pg_perf_bench.workloads import build_workload_evidence


def arguments():
    return [
        'benchmark',
        '--managed',
        '--host',
        'managed.example',
        '--port',
        '6432',
        '--user',
        'bench_owner',
        '--database',
        'bench_db',
        '--allow-database-reset',
        '--reset-mode',
        'schema',
        '--workload-profile',
        'pagila',
        '--init-fsync',
        'keep',
        '--workload-scale',
        '1.15',
        '--workload-duration-seconds',
        '1',
        '--pgbench-clients',
        '1,2',
    ]


def test_explicit_managed_mode_needs_no_metadata_and_hashes_reset_policy():
    args = build_parser().parse_args(arguments())
    config = build_runtime_config(args)
    assert config.host.connection_type == ConnectionType.MANAGED
    assert config.workload.managed and config.workload.managed_pg_info is None
    assert config.workload.init_synchronous_commit == 'keep'
    workload = config.workload.as_legacy_dict(config.host)
    assert workload['reset_mode'] == 'schema' and workload['managed']
    schema_plan = _runtime_plan(config)
    args.reset_mode = 'database'
    database_plan = _runtime_plan(build_runtime_config(args))
    assert schema_plan['plan_hash'] != database_plan['plan_hash']
    schema = build_workload_evidence(workload, [])
    database = build_workload_evidence({**workload, 'reset_mode': 'database'}, [])
    assert schema['definition_hash'] == database['definition_hash']
    assert schema['execution_hash'] != database['execution_hash']


@pytest.mark.parametrize('policy', ['off', 'local'])
def test_loader_commit_override_is_explicit_and_changes_execution_hash(policy):
    parser = build_parser()
    normal = build_runtime_config(parser.parse_args(arguments()))
    accelerated = build_runtime_config(
        parser.parse_args(arguments() + ['--init-synchronous-commit', policy])
    )
    assert accelerated.workload.init_synchronous_commit == policy
    first = build_workload_evidence(normal.workload.as_legacy_dict(normal.host), [])
    second = build_workload_evidence(accelerated.workload.as_legacy_dict(accelerated.host), [])
    assert first['definition_hash'] == second['definition_hash']
    assert first['execution_hash'] != second['execution_hash']


@pytest.mark.parametrize('profile', ['pagila', 'pagila-htap', 'imdb'])
def test_pgbench_protocol_cli_commands_report_and_join_hash(profile):
    parser = build_parser()
    cli = arguments() + ['--workload-profile', profile]
    simple = build_runtime_config(parser.parse_args(cli))
    prepared = build_runtime_config(parser.parse_args(cli + ['--pgbench-prepared']))
    evidence = []
    for config, protocol in ((simple, 'simple'), (prepared, 'prepared')):
        workload = config.workload.as_legacy_dict(config.host)
        commands = BenchmarkRunner.load_iterations_config(
            config.database.as_asyncpg_kwargs(), workload
        )
        assert all(f'-M {protocol}' in command[1] for command in commands)
        assert all('ARG_' not in command[1] for command in commands)
        summary = BenchmarkRunner.build_invocation_summary('managed', {}, workload)
        assert summary['workload']['pgbench_protocol'] == protocol
        evidence.append(build_workload_evidence(workload, commands))
    assert evidence[0]['definition_hash'] == evidence[1]['definition_hash']
    assert evidence[0]['execution_hash'] != evidence[1]['execution_hash']
    assert _runtime_plan(simple)['plan_hash'] != _runtime_plan(prepared)['plan_hash']


@pytest.mark.parametrize('option', ['-M prepared', '--protocol=prepared', '-Mprepared'])
def test_custom_command_keeps_explicit_prepared_protocol(option):
    config = build_runtime_config(
        build_parser().parse_args(
            arguments() + ['--workload-command', f'ARG_PGBENCH_PATH {option} ARG_PG_DATABASE']
        )
    )
    assert config.workload.pgbench_protocol == 'prepared'


def test_prepared_flag_requires_custom_command_to_use_protocol():
    with pytest.raises(ConfigurationError, match='ARG_PGBENCH_PROTOCOL'):
        pgbench_protocol('pgbench -M simple db', prepared=True)
    assert pgbench_protocol('pgbench db', prepared=False) == 'simple'
    assert pgbench_protocol('pgbench --protocol extended db', prepared=False) == 'extended'


@pytest.mark.parametrize('shell', ['bash -c', '/bin/bash -lc', 'sh -ec'])
@pytest.mark.parametrize('option', ['prepared', 'ARG_PGBENCH_PROTOCOL'])
def test_wrapped_protocol_in_cli_commands_and_report(shell, option):
    inner = f'ARG_PGBENCH_PATH -c ARG_PGBENCH_CLIENTS ARG_PG_DATABASE -M {option}'
    command = shell + ' ' + shlex.quote(inner)
    flags = ['--pgbench-prepared'] if option.startswith('ARG_') else []
    config = build_runtime_config(
        build_parser().parse_args(arguments() + ['--workload-command', command] + flags)
    )
    workload = config.workload.as_legacy_dict(config.host)
    assert workload['pgbench_protocol'] == 'prepared'
    commands = BenchmarkRunner.load_iterations_config(config.database.as_legacy_dict(), workload)
    assert all('-M prepared' in pair[1] and 'ARG_' not in pair[1] for pair in commands)
    summary = BenchmarkRunner.build_invocation_summary('managed', {}, workload)
    assert summary['workload']['pgbench_protocol'] == 'prepared'


@pytest.mark.parametrize(
    'command',
    [
        'pgbench db -M prepared; echo done',
        'pgbench -M prepared db\nprintf done',
        'env PGSSLMODE=require pgbench -M prepared db && echo --protocol simple',
        "echo '-M simple' | pgbench -M prepared db",
        'bash -c \'sh -c "pgbench -M prepared db"\'',
    ],
)
def test_protocol_ignores_other_shell_commands(command):
    assert pgbench_protocol(command, prepared=False) == 'prepared'


@pytest.mark.parametrize('command', ['run-bench.sh', 'bash run-bench.sh', 'pgbench -M "$MODE" db'])
def test_opaque_commands_are_not_assumed_simple(command):
    assert pgbench_protocol(command, prepared=False) == 'unknown'


def test_opaque_wrapper_can_accept_explicit_protocol_argument():
    assert pgbench_protocol('run-bench.sh ARG_PGBENCH_PROTOCOL', prepared=True) == 'prepared'
    with pytest.raises(ConfigurationError, match='same protocol'):
        pgbench_protocol('pgbench -M simple db; pgbench -M prepared db', prepared=False)


@pytest.mark.parametrize(
    'extra',
    [
        ['--init-fsync', 'off'],
        ['--init-mode', 'legacy'],
        ['--drop-os-caches'],
        ['--pg-custom-config', '/unused'],
        ['--connection-type', 'docker'],
        ['--connection-type', 'ssh'],
        ['--container-name', 'unused'],
    ],
)
def test_managed_schema_rejects_incompatible_options(extra):
    with pytest.raises(ConfigurationError):
        build_runtime_config(build_parser().parse_args(arguments() + extra))


@pytest.mark.parametrize(
    'schemas',
    [
        (),
        ('public',),
        ('pg_catalog',),
        ('pg_temp_1',),
        ('information_schema',),
        ('a', 'a'),
        ('a\x00',),
        ('é' * 32,),
    ],
)
def test_schema_reset_rejects_ambiguous_or_protected_names(schemas):
    with pytest.raises(ConfigurationError):
        validate_reset_schemas(schemas)


def test_wal_permission_failure_is_checked_when_opening_target_database(tmp_path):
    db = MagicMock(close=AsyncMock(), fetch=AsyncMock(return_value=[]))

    async def fetchval(sql, *args):
        if 'pg_is_in_recovery' in sql or 'is_superuser' in sql or 'pg_locks' in sql:
            return False
        if 'pg_current_wal_insert_lsn' in sql:
            import asyncpg

            raise asyncpg.InsufficientPrivilegeError('WAL access denied')
        return True

    db.fetchval = fetchval
    connect = AsyncMock(return_value=db)
    guard = InitializationSettings(
        MagicMock(),
        {'database': 'bench_db'},
        LoadOptions(fsync='keep'),
        'managed',
        None,
        state_dir=tmp_path,
        control_database='bench_db',
    )
    with patch('pg_perf_bench.initialization_settings.asyncpg.connect', connect):
        with pytest.raises(ConfigurationError, match='before loading data'):
            asyncio.run(guard.open())
    assert connect.call_args.kwargs['database'] == 'bench_db'
    db.close.assert_awaited_once()
