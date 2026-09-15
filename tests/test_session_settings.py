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
