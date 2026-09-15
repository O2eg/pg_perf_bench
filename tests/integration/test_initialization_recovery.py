"""Recovery across endpoint changes and replica disconnects during a real reset."""

import asyncio
import ipaddress
import logging
import os
import shlex
import socket
import time
import uuid
from unittest.mock import AsyncMock, patch

import asyncpg
import docker
import pytest

from pg_perf_bench.benchmark import BenchmarkRunner, run_command_result
from pg_perf_bench.connections.docker import DockerConnection
from pg_perf_bench.const import WORKLOAD_PROFILES_PATH, ConnectionType
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import LoadOptions
from pg_perf_bench.initialization_settings import InitializationSettings
from tests.integration.test_replication_report import postgres

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_INIT_INTEGRATION') != '1',
        reason='set PG_PERF_BENCH_INIT_INTEGRATION=1 for disposable loader tests',
    ),
]
LOGGER = logging.getLogger(__name__)


def wait_ready(container, port=5432):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if container.exec_run(['pg_isready', '-h', '127.0.0.1', '-p', str(port)]).exit_code == 0:
            return
        time.sleep(0.1)
    pytest.fail('PostgreSQL test container did not become ready')


@pytest.mark.parametrize('reopen_fsync', ['off', 'keep'])
def test_fsync_recovery_after_restart_through_another_server_address(tmp_path, reopen_fsync):
    client = docker.from_env()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    password = uuid.uuid4().hex
    container = client.containers.run(
        'postgres:18',
        ['postgres', '-p', str(port), '-c', 'listen_addresses=127.0.0.1,127.0.0.2'],
        name='pg-perf-init-recovery-' + uuid.uuid4().hex[:10],
        environment={'POSTGRES_PASSWORD': password, 'POSTGRES_DB': 'benchdb'},
        network_mode='host',
        detach=True,
    )
    try:
        wait_ready(container, port)

        async def scenario():
            conf = dict(
                host='127.0.0.1', port=port, user='postgres', password=password, database='benchdb'
            )
            transport = DockerConnection({'container_name': container.name}, {})
            await transport.start()
            first = InitializationSettings(
                LOGGER, conf, LoadOptions(), ConnectionType.DOCKER, transport, state_dir=tmp_path
            )
            second = None
            try:
                await first.open()
                await first.disable()
                journal = first.state_path
                assert await first.db.fetchval('SHOW fsync') == 'off'
                # Model a killed loader: the SQL connection dies without cleanup.
                await first.db.close()
                await asyncio.to_thread(container.restart)
                await asyncio.to_thread(wait_ready, container, port)
                second = InitializationSettings(
                    LOGGER,
                    {**conf, 'host': '127.0.0.2'},
                    LoadOptions(fsync=reopen_fsync),
                    ConnectionType.DOCKER,
                    transport,
                    state_dir=tmp_path,
                )
                await second.open()
                assert first.identity['address'] != second.identity['address']
                assert await second.db.fetchval('SHOW fsync') == 'on'
                assert await second._auto_value() is None
                assert not journal.exists()
                await second.disable()
                await second.close()
                db = await asyncpg.connect(**conf)
                try:
                    assert await db.fetchval('SHOW fsync') == 'on'
                    assert not list(tmp_path.glob('*.json'))
                finally:
                    await db.close()
            finally:
                if second is not None and second.db is not None:
                    await second.close()
                if first.db is not None:
                    await first.db.close()
                await transport.aclose()

        asyncio.run(scenario())
    finally:
        container.remove(force=True, v=True)
        client.close()


@pytest.mark.parametrize('init_mode', ['fast', 'legacy'])
def test_restricted_role_blocks_reset_until_fsync_recovery(tmp_path, init_mode):
    with postgres(18) as (container, conf):

        async def scenario():
            transport = DockerConnection({'container_name': container.name}, {})
            await transport.start()
            interrupted = InitializationSettings(
                LOGGER, conf, LoadOptions(), ConnectionType.DOCKER, transport, state_dir=tmp_path
            )
            limited = None
            try:
                await interrupted.open()
                await interrupted.db.execute(
                    "CREATE ROLE limited_loader LOGIN CREATEDB PASSWORD 'isolated-test-role'"
                )
                await interrupted.db.execute('ALTER DATABASE benchdb OWNER TO limited_loader')
                await interrupted.disable()
                journal = interrupted.state_path
                # Model a killed loader, then rerun with a different SQL role.
                await interrupted.db.close()
                role_conf = {
                    **conf,
                    'user': 'limited_loader',
                    'password': 'isolated-test-role',
                }
                workload = {
                    'init_mode': init_mode,
                    'init_fsync': 'keep',
                    'init_entrypoint': 'generator.py',
                    'workload_path': str(WORKLOAD_PROFILES_PATH / 'pagila'),
                    'workload_scale': 1.15,
                }
                with (
                    patch('pg_perf_bench.initialization_settings._STATE_DIR', tmp_path),
                    patch.object(BenchmarkRunner, 'reset_db_environment', AsyncMock()) as reset,
                    patch.object(
                        BenchmarkRunner,
                        'run_benchmark_with_evidence',
                        AsyncMock(return_value={'metrics': {'tps': 1}}),
                    ) as run,
                ):
                    with pytest.raises(
                        ConfigurationError, match='Pending fsync recovery.*superuser'
                    ):
                        await BenchmarkRunner.run_benchmark_iterations(
                            LOGGER,
                            [['init', 'workload']],
                            ConnectionType.DOCKER,
                            transport,
                            role_conf,
                            workload,
                        )
                    reset.assert_not_awaited()
                    run.assert_not_awaited()
                assert journal.exists()
                # Restore with the original privileged connection information.
                await interrupted.close()
                assert not journal.exists()
                limited = InitializationSettings(
                    LOGGER,
                    role_conf,
                    LoadOptions(fsync='keep'),
                    ConnectionType.DOCKER,
                    transport,
                    state_dir=tmp_path,
                )
                await limited.open()
                assert await limited.db.fetchval('SHOW fsync') == 'on'
                assert not await limited.db.fetchval(
                    "SELECT current_setting('is_superuser')::boolean"
                )
            finally:
                if limited is not None and limited.db is not None:
                    await limited.close()
                await interrupted.close()
                await transport.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize('reconnect', ['none', 'same_address', 'new_address'])
def test_replica_seen_before_reset_blocks_workload_until_replay(tmp_path, reconnect):
    with postgres(18) as (primary, conf):
        client = docker.from_env()
        standby = None
        network = None
        try:
            network = client.networks.create('pg-perf-init-reconnect-' + uuid.uuid4().hex[:10])
            network.connect(primary, aliases=['loader-primary'])
            result = primary.exec_run(
                [
                    'sh',
                    '-c',
                    'echo "host replication postgres 0.0.0.0/0 md5" >> "$PGDATA/pg_hba.conf"',
                ],
                user='postgres',
            )
            assert result.exit_code == 0
            primary.exec_run(
                ['psql', '-U', 'postgres', '-c', 'SELECT pg_reload_conf()'], user='postgres'
            )
            standby = client.containers.run(
                'postgres:18',
                [
                    'sh',
                    '-ec',
                    'if [ ! -f /tmp/standbydata/PG_VERSION ]; then '
                    'mkdir -m 700 /tmp/standbydata; '
                    'pg_basebackup -D /tmp/standbydata -R -X stream -C -S loader_reconnect; fi; '
                    'exec postgres -D /tmp/standbydata',
                ],
                name='pg-perf-init-reconnect-' + uuid.uuid4().hex[:10],
                user='postgres',
                network=network.name,
                environment={
                    'PGHOST': 'loader-primary',
                    'PGUSER': 'postgres',
                    'PGPASSWORD': conf['password'],
                    'PGAPPNAME': 'loader_reconnect',
                },
                detach=True,
            )
            wait_ready(standby)

            async def scenario():
                transport = DockerConnection(
                    {'container_name': primary.name}, {}, start_if_stopped=True
                )
                await transport.start()
                db = await asyncpg.connect(**conf)
                try:
                    for _ in range(100):
                        if (
                            await db.fetchval(
                                "SELECT count(*) FROM pg_stat_replication WHERE state='streaming'"
                            )
                            == 1
                        ):
                            break
                        await asyncio.sleep(0.1)
                    else:
                        pytest.fail('standby did not start streaming')
                    data_directory = await db.fetchval('SHOW data_directory')
                    original_address = await db.fetchval(
                        'SELECT client_addr::text FROM pg_stat_replication '
                        "WHERE application_name='loader_reconnect'"
                    )
                finally:
                    await db.close()
                workload = {
                    'init_mode': 'fast',
                    'init_entrypoint': 'generator.py',
                    'workload_path': str(WORKLOAD_PROFILES_PATH / 'pagila'),
                    'workload_scale': 1.15,  # Approximately 10 MiB including indexes.
                    'pg_data_path': data_directory,
                    'pg_bin_path': '/usr/lib/postgresql/18/bin',
                    'command_timeout': 20 if reconnect != 'none' else 5,
                    'system_metrics_duration': 0.1,
                    'allow_database_reset': True,
                }
                barrier_started = asyncio.Event()
                barrier_result = {}
                reset = BenchmarkRunner.reset_db_environment
                wait = InitializationSettings.wait_for_replicas

                async def reset_and_disconnect(*args):
                    await reset(*args)
                    await asyncio.to_thread(standby.stop, timeout=10)

                async def wait_at_barrier(settings):
                    barrier_started.set()
                    result = await wait(settings)
                    barrier_result.update(result)
                    return result

                async def run_after_replay(*args, **kwargs):
                    db = await asyncpg.connect(**conf)
                    try:
                        replica = await db.fetchrow(
                            'SELECT pg_wal_lsn_diff($1::text::pg_lsn, r.replay_lsn) AS lag, '
                            'r.client_addr::text AS address, s.slot_name '
                            'FROM pg_stat_replication r JOIN pg_replication_slots s '
                            "ON s.active_pid=r.pid WHERE r.application_name='loader_reconnect'",
                            barrier_result['target_lsn'],
                        )
                        assert replica['lag'] is not None and replica['lag'] <= 0
                        assert replica['slot_name'] == 'loader_reconnect'
                        if reconnect == 'new_address':
                            assert replica['address'] != original_address
                    finally:
                        await db.close()
                    return await run_command_result(*args, **kwargs)

                query = tmp_path / 'workload.sql'
                query.write_text('SELECT 1;\n')
                command = shlex.join(
                    [
                        '/usr/lib/postgresql/18/bin/pgbench',
                        '-n',
                        '-c',
                        '1',
                        '-t',
                        '2',
                        '-f',
                        str(query),
                    ]
                )
                pending = None
                try:
                    with (
                        patch.object(BenchmarkRunner, 'reset_db_environment', reset_and_disconnect),
                        patch.object(InitializationSettings, 'wait_for_replicas', wait_at_barrier),
                        patch(
                            'pg_perf_bench.initialization_settings._STATE_DIR', tmp_path / 'state'
                        ),
                        patch(
                            'pg_perf_bench.benchmark.collect_system_metrics',
                            AsyncMock(return_value={}),
                        ),
                        patch(
                            'pg_perf_bench.benchmark.run_command_result',
                            AsyncMock(side_effect=run_after_replay),
                        ) as run,
                    ):
                        pending = asyncio.create_task(
                            BenchmarkRunner.run_benchmark_iterations(
                                LOGGER,
                                [['init', command]],
                                ConnectionType.DOCKER,
                                transport,
                                conf,
                                workload,
                            )
                        )
                        await asyncio.wait_for(barrier_started.wait(), 60)
                        await asyncio.sleep(0.3)
                        assert not pending.done()
                        run.assert_not_awaited()
                        if reconnect != 'none':
                            if reconnect == 'new_address':
                                await asyncio.to_thread(network.disconnect, standby)
                                network.reload()
                                subnet = ipaddress.ip_network(
                                    network.attrs['IPAM']['Config'][0]['Subnet']
                                )
                                await asyncio.to_thread(
                                    network.connect,
                                    standby,
                                    ipv4_address=str(subnet.network_address + 42),
                                )
                            await asyncio.to_thread(standby.start)
                            result = await asyncio.wait_for(pending, 30)
                            assert (
                                result[0]['initialization']['replication_barrier']['replicas'] == 1
                            )
                            assert result[0]['metrics']['transactions'] == 2
                            run.assert_awaited_once()
                        else:
                            with pytest.raises(RuntimeError, match='Replicas did not replay'):
                                await asyncio.wait_for(pending, 15)
                            run.assert_not_awaited()
                        db = await asyncpg.connect(**conf)
                        try:
                            assert await db.fetchval('SHOW fsync') == 'on'
                        finally:
                            await db.close()
                finally:
                    if pending is not None and not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    await transport.aclose()

            asyncio.run(scenario())
        finally:
            if standby is not None:
                standby.remove(force=True, v=True)
            if network is not None:
                network.disconnect(primary, force=True)
                network.remove()
            client.close()
