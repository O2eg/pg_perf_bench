"""Shared pg_perf_bench exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PgPerfBenchError(Exception):
    """Base class for user-facing failures."""


class ConfigurationError(PgPerfBenchError):
    """The requested run configuration is invalid."""


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
