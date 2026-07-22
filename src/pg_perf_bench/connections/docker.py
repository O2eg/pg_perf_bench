"""Docker SDK host transport without event-loop blocking."""

from __future__ import annotations

import asyncio
import gzip
import io
import shlex
import tarfile
from pathlib import Path
from types import TracebackType
from typing import Any

import docker

from pg_perf_bench.executors import run_local_shell


class DockerConnection:
    # PostgreSQL runs in the container, while pg_diag OS charts and static
    # hardware inventory describe the Docker host that generates the load.
    collect_system_from_local_host = True

    def __init__(
        self,
        conn_params: dict[str, Any],
        env: dict[str, str],
        command_timeout: float = 300.0,
        start_if_stopped: bool = False,
    ) -> None:
        self.conn_params = conn_params
        self.docker_client: Any = None
        self.container: Any = None
        self.logger = None
        self.env = {str(key): str(value) for key, value in env.items()}
        self.command_timeout = float(command_timeout)
        self.start_if_stopped = start_if_stopped
        self.started_by_us = False

    async def start(self) -> None:
        await asyncio.wait_for(
            asyncio.to_thread(self._start_sync),
            timeout=min(self.command_timeout, 60.0),
        )

    def _start_sync(self) -> None:
        if self.docker_client is None:
            try:
                self.docker_client = docker.from_env()
            except docker.errors.DockerException as exc:
                raise OSError(f'Cannot access Docker daemon: {exc}') from exc
        name = self.conn_params.get('container_name')
        if not name:
            raise ValueError('Missing "container_name" in Docker connection params')
        try:
            self.container = self.docker_client.containers.get(name)
            self.container.reload()
        except docker.errors.NotFound as exc:
            raise FileNotFoundError(f'Container {name!r} not found.') from exc
        except docker.errors.DockerException as exc:
            raise OSError(f'Failed to inspect container {name!r}: {exc}') from exc
        if self.container.status != 'running':
            if not self.start_if_stopped:
                raise ConnectionError(
                    f'Container {name!r} is not running; start it explicitly before '
                    'a read-only collection run'
                )
            self.container.start()
            self.started_by_us = True

    async def stop_container(self) -> None:
        if self.container is None:
            return
        await asyncio.wait_for(
            asyncio.to_thread(self._stop_container_sync),
            timeout=min(self.command_timeout, 60.0),
        )

    def _stop_container_sync(self) -> None:
        if self.container is None:
            return
        self.container.reload()
        if self.container.status == 'running':
            self.container.stop(timeout=30)

    async def aclose(self, *, stop_container: bool = False) -> None:
        if stop_container:
            await self.stop_container()
        await asyncio.to_thread(self._close_client_sync)

    def close(self) -> None:
        """Close SDK resources without changing the container state."""
        self._close_client_sync()

    def _close_client_sync(self) -> None:
        if self.docker_client is not None:
            self.docker_client.close()
        self.container = None
        self.docker_client = None

    async def run_command(
        self,
        cmd: str,
        check: bool = False,
        timeout: float | None = None,
    ) -> str:
        deadline = self.command_timeout if timeout is None else float(timeout)
        return await asyncio.wait_for(
            asyncio.to_thread(
                self._run_command_sync,
                cmd,
                check,
                'postgres',
                deadline,
            ),
            timeout=deadline + 2.0,
        )

    async def run_command_as_root(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> str:
        deadline = self.command_timeout if timeout is None else float(timeout)
        return await asyncio.wait_for(
            asyncio.to_thread(
                self._run_command_sync,
                cmd,
                True,
                'root',
                deadline,
            ),
            timeout=deadline + 2.0,
        )

    async def run_host_command(
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
            self.logger.debug('Local Docker-host command stderr: %s', result.stderr.strip())
        return result.stdout

    def _run_command_sync(
        self,
        cmd: str,
        check: bool,
        user: str,
        timeout: float | None = None,
    ) -> str:
        if self.container is None:
            raise ConnectionError('Docker container is not initialized.')
        command = ['/bin/bash', '-lc', cmd]
        if timeout is not None:
            command = [
                'timeout',
                '--signal=TERM',
                '--kill-after=1s',
                f'{timeout:g}s',
                *command,
            ]
        result = self.container.exec_run(
            command,
            user=user,
            demux=True,
            environment=self.env,
        )
        stdout_raw, stderr_raw = result.output
        stdout = stdout_raw.decode('utf-8', 'replace') if stdout_raw else ''
        stderr = stderr_raw.decode('utf-8', 'replace') if stderr_raw else ''
        if result.exit_code in {124, 137} and timeout is not None:
            raise TimeoutError(f'Docker command timed out after {timeout:g} seconds: {cmd}')
        if check and result.exit_code != 0:
            detail = stderr.strip() or stdout.strip() or 'no output'
            raise RuntimeError(
                f'Docker command failed with exit code {result.exit_code}: {cmd}\n{detail}'
            )
        if self.logger and stderr.strip():
            self.logger.debug('Docker command stderr: %s', stderr.strip())
        return stdout

    async def send_pg_config_file(
        self,
        local_config_path: str,
        remote_data_dir: str,
    ) -> str:
        source = Path(local_config_path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f'File not found: {source}')
        if self.container is None:
            raise ConnectionError('Docker container is not initialized.')
        return await asyncio.to_thread(
            self._send_pg_config_file_sync,
            source,
            remote_data_dir,
        )

    def _send_pg_config_file_sync(self, source: Path, remote_data_dir: str) -> str:
        assert self.container is not None
        temporary_name = f'.pg_perf_bench_{source.name}'
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as archive:
            archive.add(source, arcname=temporary_name)
        tar_buffer.seek(0)
        if not self.container.put_archive('/tmp', tar_buffer.getvalue()):
            raise OSError('Failed to copy PostgreSQL configuration into container.')
        temporary_path = f'/tmp/{temporary_name}'
        destination = f'{remote_data_dir.rstrip("/")}/postgresql.conf'
        install_command = (
            'install -o postgres -g postgres -m 0640 -- '
            f'{shlex.quote(temporary_path)} {shlex.quote(destination)}'
        )
        try:
            self._run_command_sync(install_command, True, 'root')
        finally:
            self._run_command_sync(
                f'rm -f -- {shlex.quote(temporary_path)}',
                False,
                'root',
            )
        return destination

    async def copy_db_log_files(
        self,
        log_source_path: str,
        local_path: str,
        report_name: str,
    ) -> str:
        if self.container is None:
            raise ConnectionError('Docker container is not initialized.')
        archive_name = Path(report_name).name
        if archive_name != report_name or archive_name in {'', '.', '..'}:
            raise ValueError(f'Invalid log archive name: {report_name!r}')
        if not archive_name.endswith(('.tar', '.tar.gz', '.tgz')):
            archive_name += '.tar.gz'
        destination = Path(local_path) / archive_name
        await asyncio.to_thread(
            self._copy_db_log_files_sync,
            log_source_path,
            destination,
        )
        return str(destination)

    def _copy_db_log_files_sync(self, source: str, destination: Path) -> None:
        assert self.container is not None
        destination.parent.mkdir(parents=True, exist_ok=True)
        stream, _ = self.container.get_archive(source)
        opener = gzip.open if destination.name.endswith(('.tar.gz', '.tgz')) else open
        with opener(destination, 'wb') as output:
            for chunk in stream:
                output.write(chunk)

    async def __aenter__(self) -> DockerConnection:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose(stop_container=False)
