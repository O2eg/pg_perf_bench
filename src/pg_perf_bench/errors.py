"""Shared pg_perf_bench exceptions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


def exception_evidence(error: BaseException, name: str, default: Any = None) -> Any:
    """Read attached evidence through Task cancellation wrappers on Python 3.10.

    Older asyncio Tasks create a new CancelledError and keep the original in
    __context__. Do not follow unrelated exception contexts: they may describe
    a different operation. The closest explicit value, including None, wins.
    """
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if hasattr(current, name):
            return getattr(current, name)
        if isinstance(current, asyncio.CancelledError):
            for nested in (current.__context__, current.__cause__):
                if isinstance(nested, asyncio.CancelledError):
                    pending.append(nested)
    return default


class PgPerfBenchError(Exception):
    """Base class for user-facing failures."""


class ConfigurationError(PgPerfBenchError):
    """The requested run configuration is invalid."""


class PreconditionError(PgPerfBenchError):
    """A reviewed plan or required runtime precondition is missing or stale."""


class CollectionError(PgPerfBenchError):
    """Required benchmark evidence could not be collected."""


class ReportError(PgPerfBenchError):
    """A report could not be validated or persisted."""


class CommandTimeoutError(PgPerfBenchError):
    """A bounded command exceeded its deadline."""


@dataclass(frozen=True)
class CommandFailure:
    """Serializable evidence for a failed external command."""

    command: str
    returncode: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            'command': self.command,
            'returncode': self.returncode,
            'stdout': self.stdout,
            'stderr': self.stderr,
            'elapsed_seconds': self.elapsed_seconds,
        }


class CommandExecutionError(PgPerfBenchError):
    """An external command returned a non-zero exit status."""

    def __init__(self, failure: CommandFailure):
        self.failure = failure
        detail = failure.stderr.strip() or failure.stdout.strip() or 'no output'
        super().__init__(
            f'Command failed with exit code {failure.returncode}: {failure.command}\n{detail}'
        )
