from pathlib import Path
from unittest.mock import patch

import pytest

from pg_perf_bench.client_tools import ClientTool, select_latest_client_tool
from pg_perf_bench.errors import ConfigurationError


def _tool(version: tuple[int, int], path: str) -> ClientTool:
    return ClientTool(
        'pgbench', Path(path), f'pgbench (PostgreSQL) {version[0]}.{version[1]}', version
    )


def test_newest_local_pgbench_is_selected_by_default():
    with patch(
        'pg_perf_bench.client_tools.installed_client_tools',
        return_value=(_tool((17, 5), '/pg17/pgbench'), _tool((18, 4), '/pg18/pgbench')),
    ):
        assert select_latest_client_tool('pgbench').path == Path('/pg18/pgbench')


def test_explicit_older_pgbench_is_rejected():
    with (
        patch(
            'pg_perf_bench.client_tools.installed_client_tools',
            return_value=(_tool((17, 5), '/pg17/pgbench'), _tool((18, 4), '/pg18/pgbench')),
        ),
        patch(
            'pg_perf_bench.client_tools.inspect_client_tool',
            return_value=_tool((17, 5), '/pg17/pgbench'),
        ),
        pytest.raises(ConfigurationError, match='older than the newest installed'),
    ):
        select_latest_client_tool('pgbench', '/pg17/pgbench')
