import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_perf_bench.connections.docker import DockerConnection
from pg_perf_bench.connections.local import LocalConnection
from pg_perf_bench.connections.ssh import SSHConnection


def test_local_command_output_is_never_treated_as_lifecycle_action():
    output = asyncio.run(LocalConnection({}).run_command("printf 'restart'", check=True))
    assert output == 'restart'


def test_stopped_docker_container_is_not_started_for_collection():
    container = MagicMock(status='exited')
    client = MagicMock()
    client.containers.get.return_value = container
    connection = DockerConnection(
        {'container_name': 'postgres'},
        {},
        start_if_stopped=False,
    )
    connection.docker_client = client

    with pytest.raises(ConnectionError, match='not running'):
        connection._start_sync()
    container.start.assert_not_called()


def test_stopped_docker_container_can_be_started_for_benchmark():
    container = MagicMock(status='exited')
    client = MagicMock()
    client.containers.get.return_value = container
    connection = DockerConnection(
        {'container_name': 'postgres'},
        {},
        start_if_stopped=True,
    )
    connection.docker_client = client

    connection._start_sync()
    container.start.assert_called_once_with()
    assert connection.started_by_us is True


def test_ssh_environment_is_injected_without_acceptenv_dependency():
    result = MagicMock(stdout='ok', stderr='', exit_status=0)
    client = MagicMock()
    client.run = AsyncMock(return_value=result)
    connection = SSHConnection(
        {'host': 'db.example'},
        env={'ARG_PG_BIN_PATH': '/opt/postgresql/bin'},
    )
    connection.client = client

    assert asyncio.run(connection.run_command('pg_config', check=True)) == 'ok'
    remote_command = client.run.await_args.args[0]
    assert remote_command.startswith('env ARG_PG_BIN_PATH=/opt/postgresql/bin /bin/sh -c ')
    assert 'env=' not in client.run.await_args.kwargs


def test_ssh_timeout_is_reported_as_timeout_error():
    client = MagicMock()
    client.run = AsyncMock(side_effect=asyncio.TimeoutError)
    connection = SSHConnection({'host': 'db.example'}, command_timeout=0.1)
    connection.client = client

    with pytest.raises(TimeoutError, match='timed out'):
        asyncio.run(connection.run_command('sleep 10'))
