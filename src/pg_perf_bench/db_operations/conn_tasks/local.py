"""PostgreSQL service operations on the local host."""

from __future__ import annotations

import shlex

from .common import run_command


class LocalConnTasks:
    def __init__(self, db_conf, conn, logger):
        self.conn = conn
        self.pg_bin_path = db_conf['pg_bin_path']
        self.pg_data_path = db_conf['pg_data_path']
        self.logger = logger

    def _pg_ctl(self, action: str) -> str:
        pg_ctl = shlex.quote(f'{self.pg_bin_path.rstrip("/")}/pg_ctl')
        data_path = shlex.quote(str(self.pg_data_path))
        inner = f'{pg_ctl} {action} -D {data_path} -w'
        return f'su - postgres -c {shlex.quote(inner)}'

    async def stop_db(self):
        return await self.conn.run_command(self._pg_ctl('stop'), check=True)

    async def start_db(self):
        return await self.conn.run_command(self._pg_ctl('start'), check=True)

    async def sync(self):
        self.logger.debug('Flushing database host filesystems.')
        return await run_command(self.logger, 'sync', True)

    async def drop_caches(self):
        command = "sudo /bin/sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
        return await run_command(self.logger, command, True)
