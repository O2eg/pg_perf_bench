import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pg_perf_bench.initialization import LoadOptions, loader_connection
from pg_perf_bench.session_settings import close_diagnostic_connection


@pytest.mark.parametrize('policy', ['keep', 'off', 'local'])
@pytest.mark.parametrize('ending', ['success', 'error', 'cancel'])
def test_loader_preserves_defaults_and_restores_session_on_every_exit(policy, ending):
    original = {'synchronous_commit': 'remote_apply', 'search_path': 'pg_catalog'}
    settings = dict(original)
    touched = []

    class Connection:
        closed = False

        def is_closed(self):
            return self.closed

        async def fetchval(self, sql, name):
            return settings[name]

        async def execute(self, sql, name, value):
            touched.append(name)
            settings[name] = value

        async def close(self):
            self.closed = True

    db = Connection()
    connect = AsyncMock(return_value=db)

    async def scenario():
        with patch('pg_perf_bench.initialization.asyncpg.connect', connect):
            try:
                async with loader_connection(
                    {}, LoadOptions(synchronous_commit=policy), search_path='"bench", public'
                ):
                    assert settings['search_path'] == '"bench", public'
                    assert settings['synchronous_commit'] == (
                        'remote_apply' if policy == 'keep' else policy
                    )
                    if ending == 'error':
                        raise RuntimeError('load failed')
                    if ending == 'cancel':
                        raise asyncio.CancelledError()
            except RuntimeError:
                assert ending == 'error'
            except asyncio.CancelledError:
                assert ending == 'cancel'
        assert settings == original
        assert db.closed
        assert 'synchronous_commit' not in connect.call_args.kwargs['server_settings']
        if policy == 'keep':
            assert 'synchronous_commit' not in touched
        else:
            assert touched.count('synchronous_commit') == 2

    asyncio.run(scenario())


@pytest.mark.parametrize('error', [None, RuntimeError('reset failed')])
def test_diagnostic_cleanup_resets_only_its_settings_and_always_closes(error):
    class Connection:
        def is_closed(self):
            return False

    db = Connection()
    db.execute = AsyncMock(side_effect=error)
    db.close = AsyncMock()
    if error:
        with pytest.raises(RuntimeError, match='reset failed'):
            asyncio.run(close_diagnostic_connection(db))
    else:
        asyncio.run(close_diagnostic_connection(db))
    sql = db.execute.call_args.args[0]
    assert 'RESET default_transaction_read_only' in sql
    assert 'RESET statement_timeout' in sql
    assert 'RESET lock_timeout' in sql
    assert 'synchronous_commit' not in sql
    db.close.assert_awaited_once()


def test_workload_timeout_preserves_other_pgoptions(monkeypatch):
    from pg_perf_bench.session_settings import workload_environment

    monkeypatch.setenv('PGOPTIONS', '-c work_mem=64MB')
    env = workload_environment({}, statement_timeout_seconds=0.0001)
    assert env['PGOPTIONS'] == '-c work_mem=64MB -c statement_timeout=1'


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf'), 2147483.648])
def test_workload_timeout_rejects_invalid_values(timeout):
    from pg_perf_bench.errors import ConfigurationError
    from pg_perf_bench.session_settings import workload_environment

    with pytest.raises(ConfigurationError):
        workload_environment({}, statement_timeout_seconds=timeout)


def test_sql_timeout_probe_does_not_require_startup_timeout(monkeypatch):
    from types import SimpleNamespace

    from pg_perf_bench.session_settings import check_workload_session

    monkeypatch.delenv('PGOPTIONS', raising=False)
    calls = []

    async def process(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout='["imdb", "public"]' if len(calls) == 1 else '50')

    with patch('pg_perf_bench.session_settings.run_local_process', process):
        asyncio.run(
            check_workload_session(
                {},
                SimpleNamespace(schemas=['imdb']),
                psql_path='psql',
                pgbench_path='pgbench',
                timeout=10,
                pgbench_protocol='simple',
                statement_timeout_seconds=0.05,
                sql_statement_timeout=True,
            )
        )
    assert len(calls) == 2
    assert all('statement_timeout' not in kwargs['env'].get('PGOPTIONS', '') for _, kwargs in calls)
    assert 'SET statement_timeout=50;' in calls[1][0][calls[1][0].index('-c') + 1]
