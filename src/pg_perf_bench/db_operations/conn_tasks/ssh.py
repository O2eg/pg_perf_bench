"""PostgreSQL service operations through SSH."""

from __future__ import annotations

import shlex


class SSHTasks:
    def __init__(self, db_conf, conn, logger):
        self.pg_bin_path = db_conf['pg_bin_path']
        self.pg_data_path = db_conf['pg_data_path']
        self.conn = conn
        self.logger = logger

    def _pg_ctl(self, action: str) -> str:
        pg_ctl = shlex.quote(f'{self.pg_bin_path.rstrip("/")}/pg_ctl')
        data_path = shlex.quote(str(self.pg_data_path))
        return f'{pg_ctl} {action} -D {data_path} -w'

    async def stop_db(self):
        return await self.conn.run_command(self._pg_ctl('stop'), check=True)

    async def start_db(self):
        return await self.conn.run_command(self._pg_ctl('start'), check=True)

    async def sync(self):
        self.logger.debug('Flushing database host filesystems.')
        return await self.conn.run_command('sync', check=True)

    async def drop_caches(self):
        command = "sudo /bin/sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
        return await self.conn.run_command(command, check=True)
