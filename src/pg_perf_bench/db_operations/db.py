"""Safe PostgreSQL lifecycle operations for disposable benchmark databases."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from pg_perf_bench.errors import ConfigurationError

PROTECTED_DATABASES = frozenset({'postgres', 'template0', 'template1'})


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class DBTasks:
    def __init__(self, db_conf: dict[str, Any], logger):
        self.db_conf = db_conf
        self.logger = logger
        self.connect_timeout = float(db_conf.get('connect_timeout', 5.0))

    def _connection_kwargs(self, database: str) -> dict[str, Any]:
        return {
            'host': self.db_conf['host'],
            'port': int(self.db_conf['port']),
            'user': self.db_conf['user'],
            'database': database,
            'password': self.db_conf.get('password'),
            'timeout': self.connect_timeout,
        }

    @asynccontextmanager
    async def _connect(self, database: str):
        connection = await asyncpg.connect(**self._connection_kwargs(database))
        try:
            yield connection
        finally:
            await connection.close()

    def _validate_disposable_database(self) -> None:
        database = str(self.db_conf['database'])
        if database.lower() in PROTECTED_DATABASES:
            raise ConfigurationError(
                f'Refusing to recreate protected database {database!r}; '
                'select a dedicated benchmark database'
            )

    async def init_db(self) -> None:
        self._validate_disposable_database()
        database = quote_ident(str(self.db_conf['database']))
        owner = quote_ident(str(self.db_conf['user']))
        sql = f"CREATE DATABASE {database} OWNER {owner} TEMPLATE template0 ENCODING 'UTF8'"
        self.logger.debug('Creating pristine benchmark database.')
        try:
            async with self._connect('postgres') as connection:
                await connection.execute(sql)
        except Exception as exc:
            raise RuntimeError(f'Failed to create benchmark database: {exc}') from exc

    async def drop_db(self) -> None:
        self._validate_disposable_database()
        database_name = str(self.db_conf['database'])
        drop_sql = f'DROP DATABASE IF EXISTS {quote_ident(database_name)}'
        self.logger.debug('Dropping benchmark database %r.', database_name)
        try:
            async with self._connect('postgres') as connection:
                await connection.execute(
                    'SELECT pg_terminate_backend(pid) '
                    'FROM pg_stat_activity '
                    'WHERE pid <> pg_backend_pid() AND datname = $1',
                    database_name,
                )
                await connection.execute(drop_sql)
        except Exception as exc:
            raise RuntimeError(
                f'Failed to drop benchmark database {database_name!r}: {exc}'
            ) from exc

    async def check_user_db_access(self) -> bool:
        return await self._wait_for_database(str(self.db_conf['database']))

    async def check_db_access(self) -> bool:
        return await self._wait_for_database('postgres')

    async def _wait_for_database(
        self,
        database: str,
        *,
        attempts: int = 10,
        retry_delay: float = 1.0,
    ) -> bool:
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            connection = None
            try:
                connection = await asyncpg.connect(**self._connection_kwargs(database))
                await connection.fetchval('SELECT 1')
                self.logger.debug('Database %r is available.', database)
                return True
            except asyncpg.InvalidAuthorizationSpecificationError as exc:
                raise PermissionError(
                    f'PostgreSQL authentication failed for database {database!r}: {exc}'
                ) from exc
            except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                self.logger.warning(
                    'Database %r is not available. Attempt %d/%d: %s',
                    database,
                    attempt,
                    attempts,
                    exc,
                )
            finally:
                if connection is not None:
                    await connection.close()
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
        raise ConnectionError(
            f'Failed to connect to database {database!r} after {attempts} attempts: {last_error}'
        )
