"""MDB startup options and protocol preflight, based on the verified sidecar case."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.config import pgbench_protocol
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import LoadPlan
from pg_perf_bench.session_settings import check_workload_session, workload_environment

DB_CONF = {
    'host': 'db.example',
    'port': 6432,
    'user': 'bench_owner',
    'database': 'benchdb',
    'password': 'secret',
}


def test_self_managed_quotes_schemas_and_appends_public(monkeypatch):
    monkeypatch.delenv('PGOPTIONS', raising=False)
    env = workload_environment(DB_CONF, SimpleNamespace(schemas=['pagila']))
    assert env['PGOPTIONS'] == '-c search_path="pagila",\\ public'


@pytest.mark.parametrize('schema', ['pagila', 'imdb'])
def test_managed_sends_one_bare_schema_and_preserves_other_options(monkeypatch, schema):
    monkeypatch.setenv('PGOPTIONS', '-c statement_timeout=90000')
    plan = SimpleNamespace(schemas=[schema])
    env = workload_environment(DB_CONF, plan, managed=True)
    assert env['PGOPTIONS'] == f'-c statement_timeout=90000 -c search_path={schema}'
    assert env['PGDATABASE'] == 'benchdb'
    assert env['PGUSER'] == 'bench_owner'
    assert env['PGPASSWORD'] == 'secret'


@pytest.mark.parametrize(
    ('schemas', 'expected'),
    [(['bench_a', 'bench_b'], 'bench_a,bench_b'), (['Mixed Case', 'a,b'], '"Mixed\\ Case","a,b"')],
)
def test_managed_custom_names_preserve_identifier_semantics(monkeypatch, schemas, expected):
    monkeypatch.delenv('PGOPTIONS', raising=False)
    env = workload_environment(DB_CONF, SimpleNamespace(schemas=schemas), managed=True)
    assert env['PGOPTIONS'] == '-c search_path=' + expected


def test_without_plan_sets_no_startup_search_path(monkeypatch):
    monkeypatch.delenv('PGOPTIONS', raising=False)
    env = workload_environment(DB_CONF, managed=True)
    assert 'PGOPTIONS' not in env


@pytest.mark.parametrize('managed', [True, False])
@pytest.mark.parametrize('protocol', ['simple', 'prepared', 'unknown'])
def test_preflight_only_probes_prepared_when_selected(monkeypatch, managed, protocol):
    monkeypatch.delenv('PGOPTIONS', raising=False)
    plan = SimpleNamespace(schemas=['pagila'])
    schemas = ['pagila'] if managed else ['pagila', 'public']
    process = AsyncMock(
        side_effect=[
            SimpleNamespace(stdout=json.dumps(schemas)),
            SimpleNamespace(returncode=0, stderr=''),
            SimpleNamespace(returncode=0, stderr=''),
        ]
    )
    with patch('pg_perf_bench.session_settings.run_local_process', process):
        asyncio.run(
            check_workload_session(
                DB_CONF,
                plan,
                psql_path='psql',
                pgbench_path='pgbench',
                timeout=10,
                managed=managed,
                pgbench_protocol=protocol,
            )
        )
    assert process.await_count == (1 if protocol == 'simple' else 3)
    environment = workload_environment(DB_CONF, plan, managed=managed)
    for call in process.await_args_list:
        assert call.kwargs['env']['PGOPTIONS'] == environment['PGOPTIONS']
        assert call.kwargs['env']['PGDATABASE'] == 'benchdb'
    for call in process.await_args_list[1:]:
        assert call.args[0][1:4] == ['-n', '-M', 'prepared']


@pytest.mark.parametrize('actual', ['["pagila,public"]', '["pagila,other"]', 'invalid'])
def test_rewritten_or_invalid_search_path_stops_before_pgbench(actual):
    process = AsyncMock(return_value=SimpleNamespace(stdout=actual))
    with patch('pg_perf_bench.session_settings.run_local_process', process):
        with pytest.raises(ConfigurationError, match='search_path'):
            asyncio.run(
                check_workload_session(
                    DB_CONF,
                    SimpleNamespace(schemas=['pagila', 'other']),
                    psql_path='psql',
                    pgbench_path='pgbench',
                    timeout=10,
                    managed=True,
                    pgbench_protocol='prepared',
                )
            )
    assert process.await_count == 1


def test_prepared_reuse_failure_stops_before_reset():
    failed = MagicMock(returncode=0, stderr='pgbench: error: prepared statement already exists')
    failed.as_dict.return_value = {'stderr': failed.stderr}
    process = AsyncMock(
        side_effect=[
            SimpleNamespace(stdout='["pagila"]'),
            SimpleNamespace(returncode=0, stderr=''),
            failed,
        ]
    )
    reset = AsyncMock()
    with (
        patch('pg_perf_bench.session_settings.run_local_process', process),
        patch('pg_perf_bench.benchmark.load_plan', return_value=LoadPlan(('pagila',), '', (), ())),
        patch.object(BenchmarkRunner, 'reset_db_environment', reset),
    ):
        with pytest.raises(ConfigurationError, match='pool_discard=yes'):
            asyncio.run(
                BenchmarkRunner.run_benchmark_iterations(
                    MagicMock(),
                    [['init', 'unused']],
                    'managed',
                    None,
                    DB_CONF,
                    {
                        'init_mode': 'fast',
                        'init_entrypoint': 'unused.py',
                        'workload_path': '.',
                        'reset_mode': 'schema',
                        'init_fsync': 'keep',
                        'pgbench_protocol': pgbench_protocol(
                            "bash -c 'pgbench -M prepared db'", prepared=False
                        ),
                    },
                )
            )
    reset.assert_not_awaited()
