"""PostgreSQL service operations for a Docker-hosted database."""

from __future__ import annotations

from .common import run_command


class DockerTasks:
    def __init__(self, db_conf, conn, logger):
        self.conn = conn
        self.pg_bin_path = db_conf['pg_bin_path']
        self.pg_data_path = db_conf['pg_data_path']
        self.logger = logger

    async def stop_db(self):
        await self.conn.stop_container()
        return True

    async def start_db(self):
        await self.conn.start()
        return True

    async def sync(self):
        self.logger.debug('Flushing Docker host filesystems.')
        return await run_command(self.logger, 'sync', True)

    async def drop_caches(self):
        command = "sudo /bin/sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
        return await run_command(self.logger, command, True)
