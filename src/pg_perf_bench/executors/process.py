"""Bounded local process execution with process-group cleanup."""

from __future__ import annotations

import asyncio
import math
import os
import shlex
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from pg_perf_bench.contracts import redact_text
from pg_perf_bench.errors import (
    CommandExecutionError,
    CommandFailure,
    CommandTimeoutError,
)

PROCESS_STOP_GRACE_SECONDS = 0.5
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    started_at: str
    elapsed_seconds: float

    @property
    def command(self) -> str:
        return shlex.join(self.argv)

    def as_dict(self, *, secrets: Sequence[str | None] = ()) -> dict[str, object]:
        return {
            'command': redact_text(self.command, tuple(secrets)),
            'returncode': self.returncode,
            'stdout': redact_text(self.stdout, tuple(secrets)),
            'stderr': redact_text(self.stderr, tuple(secrets)),
            'started_at': self.started_at,
            'elapsed_seconds': self.elapsed_seconds,
        }


def _validated_timeout(timeout: float | None) -> float:
    value = DEFAULT_COMMAND_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('command timeout must be a positive finite number')
    return value


async def run_local_process(
    arguments: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    input_text: str | None = None,
    secrets: Sequence[str | None] = (),
) -> ProcessResult:
    argv = tuple(str(argument) for argument in arguments)
    if not argv or not argv[0]:
        raise ValueError('command arguments must not be empty')
    deadline = _validated_timeout(timeout)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=1024 * 1024,
    )
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(
            process.communicate(input_text.encode() if input_text is not None else None),
            timeout=deadline,
        )
    except asyncio.TimeoutError as exc:
        await _stop_process_group(process)
        command = redact_text(shlex.join(argv), tuple(secrets))
        raise CommandTimeoutError(
            f'Command timed out after {deadline:g} seconds: {command}'
        ) from exc
    except BaseException:
        await _stop_process_group(process)
        raise

    result = ProcessResult(
        argv=argv,
        returncode=int(process.returncode or 0),
        stdout=stdout_raw.decode('utf-8', errors='replace'),
        stderr=stderr_raw.decode('utf-8', errors='replace'),
        started_at=started_at,
        elapsed_seconds=round(time.perf_counter() - started, 6),
    )
    if check and result.returncode != 0:
        raise CommandExecutionError(
            CommandFailure(
                command=redact_text(result.command, tuple(secrets)),
                returncode=result.returncode,
                stdout=redact_text(result.stdout, tuple(secrets)),
                stderr=redact_text(result.stderr, tuple(secrets)),
                elapsed_seconds=result.elapsed_seconds,
            )
        )
    return result


async def run_local_shell(
    command: str,
    *,
    timeout: float | None = None,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    secrets: Sequence[str | None] = (),
) -> ProcessResult:
    if not command.strip():
        raise ValueError('command must not be empty')
    return await run_local_process(
        ('/bin/bash', '-lc', command),
        timeout=timeout,
        check=check,
        env=env,
        cwd=cwd,
        secrets=secrets,
    )


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()
