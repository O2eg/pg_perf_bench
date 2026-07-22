"""Discovery and compatibility evidence for local PostgreSQL client tools."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pg_perf_bench.errors import ConfigurationError

SUPPORTED_SERVER_MAJORS = tuple(range(10, 19))
_VERSION_RE = re.compile(r'\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?')


@dataclass(frozen=True)
class ClientTool:
    name: str
    path: Path
    version_text: str
    version: tuple[int, int]

    @property
    def major(self) -> int:
        return self.version[0]

    def as_dict(self) -> dict[str, object]:
        return {
            'name': self.name,
            'path': str(self.path),
            'version': '.'.join(str(part) for part in self.version),
            'version_text': self.version_text,
            'major': self.major,
        }


def _candidate_paths(name: str) -> Iterable[Path]:
    from_path = shutil.which(name)
    if from_path:
        yield Path(from_path)
    root = Path('/usr/lib/postgresql')
    if root.is_dir():
        yield from root.glob(f'*/bin/{name}')


def inspect_client_tool(name: str, value: str | Path) -> ClientTool:
    raw_path = Path(value).expanduser()
    executable = Path(shutil.which(str(value)) or raw_path).absolute()
    if not executable.is_file():
        raise ConfigurationError(f'{name} executable does not exist: {value}')
    try:
        completed = subprocess.run(
            [str(executable), '--version'],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigurationError(f'cannot execute {name} {executable}: {exc}') from exc
    output = (completed.stdout or completed.stderr).strip()
    match = _VERSION_RE.search(output)
    if completed.returncode != 0 or match is None:
        raise ConfigurationError(
            f'cannot determine PostgreSQL version of {name} {executable}: {output or "no output"}'
        )
    return ClientTool(
        name=name,
        path=executable,
        version_text=output,
        version=(int(match.group(1)), int(match.group(2) or 0)),
    )


def installed_client_tools(name: str) -> tuple[ClientTool, ...]:
    inspected: dict[Path, ClientTool] = {}
    for candidate in _candidate_paths(name):
        try:
            tool = inspect_client_tool(name, candidate)
        except ConfigurationError:
            continue
        inspected[tool.path] = tool
    return tuple(sorted(inspected.values(), key=lambda item: (item.version, str(item.path))))


def select_latest_client_tool(name: str, requested: str | None = None) -> ClientTool:
    installed = installed_client_tools(name)
    if not installed:
        raise ConfigurationError(
            f'no local {name} executable was found in PATH or /usr/lib/postgresql/*/bin'
        )
    latest = installed[-1]
    if requested is None:
        return latest
    selected = inspect_client_tool(name, requested)
    if selected.version < latest.version:
        raise ConfigurationError(
            f'{name} {selected.version_text!r} is older than the newest installed local '
            f'client {latest.version_text!r} at {latest.path}; use the newest local client'
        )
    return selected


def select_local_clients(
    pgbench_path: str | None,
    psql_path: str | None,
) -> tuple[ClientTool, ClientTool]:
    """Select newest local pgbench and a same-major local psql."""
    pgbench = select_latest_client_tool('pgbench', pgbench_path)
    if psql_path is not None:
        psql = select_latest_client_tool('psql', psql_path)
    else:
        sibling = pgbench.path.with_name('psql')
        psql = (
            inspect_client_tool('psql', sibling)
            if sibling.is_file()
            else select_latest_client_tool('psql')
        )
    if psql.major != pgbench.major:
        raise ConfigurationError(
            f'local pgbench major {pgbench.major} and psql major {psql.major} must match'
        )
    return pgbench, psql


def server_major_from_version_num(version_num: int) -> int:
    major = int(version_num) // 10000
    if major not in SUPPORTED_SERVER_MAJORS:
        allowed = f'{SUPPORTED_SERVER_MAJORS[0]}-{SUPPORTED_SERVER_MAJORS[-1]}'
        raise ConfigurationError(
            f'PostgreSQL server major {major} is unsupported; pg_perf_bench supports {allowed}'
        )
    return major
