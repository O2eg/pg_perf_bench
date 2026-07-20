import asyncio
import sys

import pytest

from pg_perf_bench.errors import CommandExecutionError, CommandTimeoutError
from pg_perf_bench.executors import run_local_process, run_local_shell


def test_process_result_preserves_stdout_stderr_and_exit_code():
    async def scenario():
        return await run_local_shell(
            "printf 'out'; printf 'err' >&2; exit 3",
            check=False,
        )

    result = asyncio.run(scenario())
    assert result.returncode == 3
    assert result.stdout == 'out'
    assert result.stderr == 'err'
    assert result.elapsed_seconds >= 0


def test_failure_redacts_known_secrets():
    secret = 'do-not-leak'

    async def scenario():
        await run_local_shell(
            f"printf '{secret}' >&2; exit 4",
            secrets=(secret,),
        )

    with pytest.raises(CommandExecutionError) as raised:
        asyncio.run(scenario())
    assert secret not in str(raised.value)
    assert raised.value.failure.stderr == '***'


def test_timeout_is_bounded_and_uses_specific_error():
    async def scenario():
        await run_local_process(
            (sys.executable, '-c', 'import time; time.sleep(10)'),
            timeout=0.05,
        )

    with pytest.raises(CommandTimeoutError, match='timed out'):
        asyncio.run(scenario())
