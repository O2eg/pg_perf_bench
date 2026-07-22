import asyncio
from unittest.mock import AsyncMock

from pg_perf_bench.report.commands import _run_transport_command


def test_docker_capable_transport_runs_sudo_inventory_as_root_without_sudo():
    connection = AsyncMock()
    connection.run_command_as_root.return_value = '[]'

    result = asyncio.run(
        _run_transport_command(
            connection,
            'sudo -n lshw -class system -json\n',
            {'timeout_seconds': 15},
        )
    )

    assert result == '[]'
    connection.run_command_as_root.assert_awaited_once_with(
        'lshw -class system -json\n',
        timeout=15,
    )
    connection.run_command.assert_not_awaited()


def test_non_privileged_transport_preserves_sudo_command():
    class RemoteConnection:
        run_command = AsyncMock(return_value='[]')

    connection = RemoteConnection()
    result = asyncio.run(
        _run_transport_command(
            connection,
            'sudo -n lshw -class system -json\n',
            {},
        )
    )

    assert result == '[]'
    connection.run_command.assert_awaited_once_with(
        'sudo -n lshw -class system -json\n',
        True,
    )


def test_docker_system_inventory_is_collected_from_local_host():
    class DockerHostConnection:
        collect_system_from_local_host = True
        run_host_command = AsyncMock(return_value='[]')
        run_command_as_root = AsyncMock()
        run_command = AsyncMock()

    connection = DockerHostConnection()
    result = asyncio.run(
        _run_transport_command(
            connection,
            'sudo -n lshw -class system -json\n',
            {'timeout_seconds': 15},
        )
    )

    assert result == '[]'
    connection.run_host_command.assert_awaited_once_with(
        'lshw -class system -json\n',
        True,
        timeout=15,
    )
    connection.run_command_as_root.assert_not_awaited()
    connection.run_command.assert_not_awaited()


def test_docker_pg_config_is_collected_inside_database_container():
    class DockerHostConnection:
        collect_system_from_local_host = True
        run_host_command = AsyncMock()
        run_command_as_root = AsyncMock()
        run_command = AsyncMock(return_value='BINDIR = /usr/lib/postgresql/10/bin')

    connection = DockerHostConnection()
    result = asyncio.run(
        _run_transport_command(
            connection,
            '"${ARG_PG_BIN_PATH%/}/pg_config"\n',
            {'shell_command_file': 'pg_config.sh'},
        )
    )

    assert result == 'BINDIR = /usr/lib/postgresql/10/bin'
    connection.run_command.assert_awaited_once()
    connection.run_host_command.assert_not_awaited()
