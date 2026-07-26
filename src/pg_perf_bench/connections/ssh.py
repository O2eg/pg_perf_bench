"""AsyncSSH host transport with native local forwarding."""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any

import asyncssh


class SSHConnection:
    def __init__(
        self,
        conn_params: dict[str, Any],
        tunnel_params: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        command_timeout: float = 300.0,
    ) -> None:
        self.conn_params = {key: value for key, value in conn_params.items() if value is not None}
        self.tunnel_params = tunnel_params
        self.env = {str(key): str(value) for key, value in (env or {}).items()}
        self.command_timeout = float(command_timeout)
        self.client: asyncssh.SSHClientConnection | None = None
        self.tunnel: Any = None
        self.logger = None

    async def start(self) -> None:
        if self.client is not None:
            return
        try:
            self.client = await asyncssh.connect(**self.conn_params)
            if self.tunnel_params:
                self.tunnel = await self.client.forward_local_port(
                    self.tunnel_params['local_host'],
                    int(self.tunnel_params['local_port']),
                    self.tunnel_params['remote_host'],
                    int(self.tunnel_params['remote_port']),
                )
        except asyncssh.PermissionDenied as exc:
            await self.aclose()
            raise PermissionError(
                'Verify the SSH user and selected key or agent identity for remote server access.'
            ) from exc
        except (asyncssh.ConnectionLost, OSError, asyncio.TimeoutError) as exc:
            await self.aclose()
            raise ConnectionError(f'SSH connection failed: {exc}') from exc
        except Exception:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self.tunnel is not None:
            self.tunnel.close()
            await self.tunnel.wait_closed()
            self.tunnel = None
        if self.client is not None:
            self.client.close()
            await self.client.wait_closed()
            self.client = None

    def close(self) -> None:
        """Start non-blocking close for compatibility with older callers."""
        if self.tunnel is not None:
            self.tunnel.close()
            self.tunnel = None
        if self.client is not None:
            self.client.close()

    async def run_command(
        self,
        cmd: str,
        check: bool = False,
        timeout: float | None = None,
    ) -> str:
        if self.client is None:
            raise ConnectionError('SSH client not initialized.')
        deadline = self.command_timeout if timeout is None else float(timeout)
        environment = ' '.join(f'{key}={shlex.quote(value)}' for key, value in self.env.items())
        remote_command = cmd
        if environment:
            remote_command = f'env {environment} /bin/sh -c {shlex.quote(cmd)}'
        try:
            result = await asyncio.wait_for(
                self.client.run(remote_command, check=False),
                timeout=deadline,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f'SSH command timed out after {deadline:g} seconds: {cmd}') from exc
        stdout = str(result.stdout or '')
        stderr = str(result.stderr or '')
        if check and result.exit_status != 0:
            detail = stderr.strip() or stdout.strip() or 'no output'
            raise RuntimeError(
                f'Command failed over SSH with exit code {result.exit_status}: {cmd}\n{detail}'
            )
        if self.logger and stderr.strip():
            self.logger.debug('SSH command stderr: %s', stderr.strip())
        return stdout

    async def send_pg_config_file(
        self,
        local_config_path: str,
        remote_data_dir: str,
    ) -> str:
        if self.client is None:
            raise ConnectionError('SSH client not initialized.')
        source = Path(local_config_path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f'Local config does not exist: {source}')
        remote_config_path = f'{remote_data_dir.rstrip("/")}/postgresql.conf'
        temporary_path = f'{remote_config_path}.pg_perf_bench.tmp'
        try:
            async with self.client.start_sftp_client() as sftp_client:
                await sftp_client.put(str(source), temporary_path)
                await sftp_client.rename(temporary_path, remote_config_path)
            return remote_config_path
        except Exception:
            try:
                await self.run_command(
                    f'rm -f -- {shlex.quote(temporary_path)}',
                    check=False,
                )
            except Exception:
                pass
            raise

    async def copy_db_log_files(
        self,
        log_source_path: str,
        local_path: str,
        report_name: str,
    ) -> str:
        if self.client is None:
            raise ConnectionError('SSH client not initialized.')
        archive_name = Path(report_name).name
        if archive_name != report_name or archive_name in {'', '.', '..'}:
            raise ValueError(f'Invalid log archive name: {report_name!r}')
        if not archive_name.endswith(('.tar.gz', '.tgz')):
            archive_name += '.tar.gz'
        destination_dir = Path(local_path)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / archive_name
        temporary_dir = (
            await self.run_command(
                'mktemp -d /tmp/pg_perf_bench_logs.XXXXXX',
                check=True,
            )
        ).strip()
        if not temporary_dir.startswith('/tmp/pg_perf_bench_logs.'):
            raise RuntimeError(f'Unexpected remote temporary path: {temporary_dir!r}')
        remote_archive = f'{temporary_dir}/{archive_name}'
        source_parent = os.path.dirname(log_source_path.rstrip('/')) or '/'
        source_name = os.path.basename(log_source_path.rstrip('/'))
        try:
            await self.run_command(
                'tar -czf '
                f'{shlex.quote(remote_archive)} '
                f'--directory={shlex.quote(source_parent)} '
                f'-- {shlex.quote(source_name)}',
                check=True,
            )
            async with self.client.start_sftp_client() as sftp_client:
                await sftp_client.get(remote_archive, str(destination))
            return str(destination)
        finally:
            await self.run_command(
                f'rm -rf -- {shlex.quote(temporary_dir)}',
                check=False,
            )

    async def __aenter__(self) -> SSHConnection:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.aclose()
