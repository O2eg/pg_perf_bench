"""Compatibility helpers for bounded local command execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pg_perf_bench.executors import ProcessResult, run_local_shell


async def run_command_result(
    logger,
    command: str,
    check: bool = True,
    *,
    timeout: float = 300.0,
    env: Mapping[str, str] | None = None,
    secrets: Sequence[str | None] = (),
) -> ProcessResult:
    result = await run_local_shell(
        command,
        timeout=timeout,
        check=check,
        env=env,
        secrets=secrets,
    )
    if result.stderr.strip():
        logger.debug('Command stderr: %s', result.stderr.strip())
    return result


async def run_command(
    logger,
    command: str,
    check: bool = True,
    *,
    timeout: float = 300.0,
    env: Mapping[str, str] | None = None,
    secrets: Sequence[str | None] = (),
) -> str:
    result = await run_command_result(
        logger,
        command,
        check,
        timeout=timeout,
        env=env,
        secrets=secrets,
    )
    return result.stdout
