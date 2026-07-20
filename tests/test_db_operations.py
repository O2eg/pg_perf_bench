import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.db_operations.db import DBTasks, quote_ident
from pg_perf_bench.errors import ConfigurationError


def _tasks(database='bench_db'):
    return DBTasks(
        {
            'host': '127.0.0.1',
            'port': 5432,
            'user': 'postgres',
            'password': 'secret',
            'database': database,
            'connect_timeout': 0.1,
        },
        MagicMock(),
    )


def test_identifier_quoting_and_protected_database_guard():
    assert quote_ident('odd"name') == '"odd""name"'
    with pytest.raises(ConfigurationError, match='protected database'):
        asyncio.run(_tasks('template1').drop_db())


def test_connection_retry_handles_os_errors_without_blocking_loop():
    connection = MagicMock()
    connection.fetchval = AsyncMock(return_value=1)
    connection.close = AsyncMock()
    connect = AsyncMock(side_effect=[OSError('not ready'), connection])

    async def scenario():
        with (
            patch('pg_perf_bench.db_operations.db.asyncpg.connect', connect),
            patch(
                'pg_perf_bench.db_operations.db.asyncio.sleep',
                new=AsyncMock(),
            ) as sleep,
        ):
            assert await _tasks()._wait_for_database('postgres', attempts=2, retry_delay=0.01)
            sleep.assert_awaited_once_with(0.01)

    asyncio.run(scenario())
    connection.close.assert_awaited_once()
