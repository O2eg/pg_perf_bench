"""Local host transport."""

from __future__ import annotations

import asyncio
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from types import TracebackType

from pg_perf_bench.executors import run_local_shell


class LocalConnection:
    def __init__(self, env: dict[str, str], command_timeout: float = 300.0) -> None:
        self.logger = None
        self.command_timeout = float(command_timeout)
        self.env = os.environ.copy()
        self.env.update({str(key): str(value) for key, value in env.items()})

    async def start(self) -> None:
        if self.logger:
            self.logger.debug('Local transport ready.')

    async def close(self) -> None:
        if self.logger:
            self.logger.debug('Local transport closed.')

    async def run_command(
        self,
        cmd: str,
        check: bool = False,
        timeout: float | None = None,
    ) -> str:
        result = await run_local_shell(
            cmd,
            timeout=self.command_timeout if timeout is None else timeout,
            check=check,
            env=self.env,
        )
        if self.logger and result.stderr.strip():
            self.logger.debug('Local command stderr: %s', result.stderr.strip())
        return result.stdout

    async def send_pg_config_file(
        self,
        local_config_path: str,
        remote_data_dir: str,
    ) -> str:
        source = Path(local_config_path).expanduser()
        destination_dir = Path(remote_data_dir).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f'Local file not found: {source}')
        if not destination_dir.is_dir():
            raise FileNotFoundError(f'Destination directory not found: {destination_dir}')

        def copy_atomically() -> str:
            destination = destination_dir / 'postgresql.conf'
            fd, temporary_name = tempfile.mkstemp(
                prefix='.postgresql.conf.',
                dir=destination_dir,
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return str(destination)

        return await asyncio.to_thread(copy_atomically)

    async def copy_db_log_files(
        self,
        log_source_path: str,
        local_path: str,
        report_name: str,
    ) -> str:
        source = Path(log_source_path)
        destination_dir = Path(local_path)
        archive_name = Path(report_name).name
        if archive_name != report_name or archive_name in {'', '.', '..'}:
            raise ValueError(f'Invalid log archive name: {report_name!r}')
        if not archive_name.endswith(('.tar.gz', '.tgz')):
            archive_name += '.tar.gz'

        def archive_logs() -> str:
            if not source.is_dir():
                raise FileNotFoundError(
                    f'Log source path does not exist or is not a directory: {source}'
                )
            if not any(source.iterdir()):
                raise ValueError(f'Log source directory is empty: {source}')
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / archive_name
            with tarfile.open(destination, 'w:gz') as archive:
                archive.add(source, arcname=source.name)
            return str(destination)

        return await asyncio.to_thread(archive_logs)

    async def __aenter__(self) -> LocalConnection:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()
