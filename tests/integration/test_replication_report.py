"""Opt-in PostgreSQL 10-18 SQL compatibility and real streaming-replication evidence."""

import asyncio
import json
import os
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import asyncpg
import docker
import pytest

from pg_perf_bench.const import REPORT_TEMPLATE_FOLDER
from pg_perf_bench.report.commands import fill_info_report
from pg_perf_bench.report.processing import get_report_structure, save_report

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_REPLICATION_INTEGRATION') != '1',
        reason='set PG_PERF_BENCH_REPLICATION_INTEGRATION=1 to start PostgreSQL containers',
    ),
]


@contextmanager
def postgres(major):
    client = docker.from_env()
    image = f'postgres:{major}'
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        client.close()
        pytest.skip(f'Integration image is not installed: {image}')
    password = uuid.uuid4().hex
    # Docker assigns a new ephemeral published port on container restart.
    # Request a concrete free port so the benchmark connection remains valid.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        published_port = listener.getsockname()[1]
    container = client.containers.run(
        image,
        ['postgres', '-c', 'wal_level=logical', '-c', 'shared_buffers=32MB'],
        name='pg-perf-replication-' + uuid.uuid4().hex[:10],
        environment={'POSTGRES_PASSWORD': password, 'POSTGRES_DB': 'benchdb'},
        ports={'5432/tcp': ('127.0.0.1', published_port)},
        detach=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = container.exec_run(['pg_isready', '-h', '127.0.0.1', '-U', 'postgres'])
            if ready.exit_code == 0:
                break
            time.sleep(0.2)
        else:
            pytest.fail(container.logs(tail=30).decode())
        container.reload()
        port = int(container.attrs['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
        yield (
            container,
            {
                'host': '127.0.0.1',
                'port': port,
                'user': 'postgres',
                'password': password,
                'database': 'benchdb',
            },
        )
    finally:
        container.remove(force=True, v=True)
        client.close()


async def collect(db, expected_errors=()):
    section = get_report_structure(REPORT_TEMPLATE_FOLDER / 'benchmark_report_struct.json')[
        'sections'
    ]['replication']
    report = {
        'header': 'Replication test',
        'report_name': 'replication',
        'sections': {'replication': section},
    }
    await fill_info_report(MagicMock(), None, db, {}, report)
    for name, item in section['reports'].items():
        if name in expected_errors:
            assert item['collection_status'] == 'error'
            assert 'permission denied' in item['reason']
            continue
        assert item['collection_status'] in {'ok', 'empty'}, (name, item.get('reason'))
    # SQL output must consist of JSON-native cells: no datetime, LSN or Decimal objects.
    json.dumps(report, allow_nan=False)
    return report


def rows(report, name):
    item = report['sections']['replication']['reports'][name]
    if item['collection_status'] == 'empty':
        return []
    return [dict(zip(item['theader'], row, strict=True)) for row in item['data']]


def mode(report):
    return {row['property']: row['value'] for row in rows(report, 'replication_mode')}


async def configure_policy(db, value):
    # All values come from test constants, never user input.
    await db.execute("ALTER SYSTEM SET synchronous_standby_names = '" + value + "'")
    await db.fetchval('SELECT pg_reload_conf()')
    for _ in range(100):
        if await db.fetchval("SELECT current_setting('synchronous_standby_names')") == value:
            return
        await asyncio.sleep(0.05)
    pytest.fail('synchronous_standby_names was not reloaded')


@pytest.mark.parametrize('major', range(10, 19))
def test_replication_sql_versions_slots_policies_overrides_and_permissions(major, tmp_path):
    with postgres(major) as (_container, connection):

        async def scenario():
            db = await asyncpg.connect(**connection)
            try:
                empty = await collect(db)
                assert mode(empty)['Synchronous standby policy'] == 'disabled'
                assert mode(empty)['Connected WAL senders'] == '0'
                assert not rows(empty, 'replication_slots')
                await db.execute("SET synchronous_commit = 'local'")
                await db.execute('CREATE ROLE bench_reader LOGIN')
                await db.execute("ALTER ROLE bench_reader PASSWORD 'test-reader-password'")
                await db.execute("ALTER ROLE bench_reader SET synchronous_commit = 'local'")
                await db.execute("ALTER DATABASE benchdb SET synchronous_commit = 'remote_write'")
                await db.execute(
                    "ALTER ROLE bench_reader IN DATABASE benchdb SET synchronous_commit = 'off'"
                )
                await db.execute(
                    "SELECT pg_create_physical_replication_slot('physical_slot', true)"
                )
                await db.execute(
                    "SELECT pg_create_logical_replication_slot('logical_slot', 'test_decoding')"
                )
                await db.execute(
                    'CREATE SUBSCRIPTION disabled_sub CONNECTION '
                    "'host=invalid password=private-sub-password' "
                    'PUBLICATION unused_pub WITH (connect=false, enabled=false, create_slot=false)'
                )
                await db.execute('CREATE TABLE wal_data AS SELECT generate_series(1, 100) AS id')
                report = await collect(db)
                slot_rows = rows(report, 'replication_slots')
                assert {row['slot_type'] for row in slot_rows} == {'logical', 'physical'}
                assert all(row['wal_distance_bytes'] >= 0 for row in slot_rows)
                assert all(row['active'] is False for row in slot_rows)
                assert all((row['wal_status'] is not None) == (major >= 13) for row in slot_rows)
                assert all((row['two_phase'] is not None) == (major >= 14) for row in slot_rows)
                assert all((row['failover'] is not None) == (major >= 17) for row in slot_rows)
                assert rows(report, 'replication_subscriptions')[0]['subenabled'] is False
                assert len(rows(report, 'replication_commit_overrides')) == 3
                assert 'private-sub-password' not in json.dumps(report)
                for policy, selection, required in [
                    ('replica1', 'priority (FIRST)', '1'),
                    ('2 ("replica,one", replica2)', 'priority (FIRST)', '2'),
                    ('FIRST 1 (replica1, replica2)', 'priority (FIRST)', '1'),
                    ('ANY 2 ("replica,one", replica2, *)', 'quorum (ANY)', '2'),
                ]:
                    await configure_policy(db, policy)
                    observed = mode(await collect(db))
                    assert observed['Synchronous standby policy'] == selection
                    assert observed['Required synchronous standbys'] == required
                    assert observed['Current synchronous senders'] == '0'
                    assert observed['Remote commit acknowledgement (collector session)'] == (
                        'no remote commit wait'
                    )
                limited = await asyncpg.connect(
                    **{
                        **connection,
                        'user': 'bench_reader',
                        'password': 'test-reader-password',
                        'server_settings': {'default_transaction_read_only': 'on'},
                    }
                )
                try:
                    restricted = await collect(
                        limited,
                        expected_errors=('replication_subscriptions',) if major < 14 else (),
                    )
                    observed = mode(restricted)
                    assert observed['synchronous_commit (collector session)'] == 'off'
                    assert observed['Statistics visibility'].startswith('restricted')
                    assert observed['Current synchronous senders'] is None
                    assert observed['Sessions waiting in SyncRep'] is None
                    assert len(rows(restricted, 'replication_slots')) == 2
                finally:
                    await limited.close()
                save_report(MagicMock(), report, tmp_path)
                assert 'Replication slots' in (tmp_path / 'replication.html').read_text()
            finally:
                await db.close()

        asyncio.run(scenario())


def test_connected_standby_async_sync_quorum_and_benchmark(tmp_path):
    with postgres(18) as (primary, connection):
        client = docker.from_env()
        standby = None
        try:

            async def setup():
                db = await asyncpg.connect(**connection)
                try:
                    await db.execute(
                        "SELECT pg_create_physical_replication_slot('standby_slot', true)"
                    )
                finally:
                    await db.close()

            asyncio.run(setup())
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
            primary.reload()
            primary_ip = primary.attrs['NetworkSettings']['Networks']['bridge']['IPAddress']
            standby = client.containers.run(
                'postgres:18',
                [
                    'sh',
                    '-ec',
                    'mkdir -m 700 /tmp/standbydata; '
                    'pg_basebackup -D /tmp/standbydata -R -X stream -S standby_slot; '
                    "exec postgres -D /tmp/standbydata -c listen_addresses='*'",
                ],
                name='pg-perf-standby-' + uuid.uuid4().hex[:10],
                user='postgres',
                environment={
                    'PGHOST': primary_ip,
                    'PGUSER': 'postgres',
                    'PGPASSWORD': connection['password'],
                    'PGAPPNAME': 'bench_standby',
                },
                ports={'5432/tcp': ('127.0.0.1', None)},
                detach=True,
            )
            # pg_basebackup also opens WAL senders. Wait for the actual standby
            # server before inspecting its persistent replication connection.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                ready = standby.exec_run(['pg_isready', '-h', '127.0.0.1', '-U', 'postgres'])
                if ready.exit_code == 0:
                    break
                time.sleep(0.2)
            else:
                pytest.fail(standby.logs(tail=30).decode())

            async def scenario():
                db = await asyncpg.connect(**connection)
                try:
                    for _ in range(150):
                        if await db.fetchval(
                            'SELECT count(*) FROM pg_stat_replication r '
                            'JOIN pg_replication_slots s ON s.active_pid = r.pid '
                            "WHERE r.state = 'streaming' AND s.slot_name = 'standby_slot'"
                        ):
                            break
                        await asyncio.sleep(0.2)
                    else:
                        pytest.fail(standby.logs(tail=30).decode())
                    report = await collect(db)
                    sender = rows(report, 'replication_senders')[0]
                    assert sender['sender_kind'] == 'physical'
                    assert sender['slot_name'] == 'standby_slot'
                    assert sender['sync_state'] == 'async'
                    assert sender['application_name'] == 'bench_standby'
                    for policy, state in [
                        ('FIRST 1 (bench_standby)', 'sync'),
                        ('ANY 1 (bench_standby)', 'quorum'),
                    ]:
                        await configure_policy(db, policy)
                        for _ in range(100):
                            report = await collect(db)
                            if rows(report, 'replication_senders')[0]['sync_state'] == state:
                                break
                            await asyncio.sleep(0.1)
                        else:
                            pytest.fail('Standby did not enter ' + state)
                        assert mode(report)[
                            'Remote commit acknowledgement (collector session)'
                        ] == ('remote WAL flush')
                    standby.reload()
                    standby_port = int(
                        standby.attrs['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
                    )
                    receiver_db = await asyncpg.connect(**{**connection, 'port': standby_port})
                    try:
                        receiver = await collect(receiver_db)
                        assert mode(receiver)['Server role'] == 'standby'
                        assert (
                            rows(receiver, 'replication_receiver')[0]['slot_name'] == 'standby_slot'
                        )
                        assert (
                            rows(receiver, 'replication_receiver')[0]['sender_host'] == primary_ip
                        )
                    finally:
                        await receiver_db.close()
                finally:
                    await db.close()

            asyncio.run(scenario())

            # Full benchmark: restarting the primary must preserve/report its real replica.
            import subprocess
            import sys

            env = {**os.environ, 'PGPASSWORD': connection['password']}
            result = subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'pg_perf_bench',
                    'benchmark',
                    '--connection-type',
                    'docker',
                    '--container-name',
                    primary.name,
                    '--allow-database-reset',
                    '--host',
                    '127.0.0.1',
                    '--port',
                    str(connection['port']),
                    '--user',
                    'postgres',
                    '--database',
                    'bench_workload',
                    '--pg-data-path',
                    '/var/lib/postgresql/18/docker',
                    '--pg-bin-path',
                    '/usr/lib/postgresql/18/bin',
                    '--benchmark-type',
                    'default',
                    '--pgbench-clients',
                    '1',
                    '--init-command',
                    'ARG_PGBENCH_PATH -i -s 1 -h ARG_PG_HOST -p ARG_PG_PORT '
                    '-U postgres ARG_PG_DATABASE',
                    '--workload-command',
                    'ARG_PGBENCH_PATH -T 1 -c 1 -j 1 '
                    '-h ARG_PG_HOST -p ARG_PG_PORT -U postgres ARG_PG_DATABASE',
                    '--out',
                    str(tmp_path),
                    '--log-dir',
                    str(tmp_path / 'logs'),
                    '--report-name',
                    'sync-benchmark',
                    '--command-timeout',
                    '60',
                ],
                cwd=Path(__file__).parents[2],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert result.returncode in {0, 5}, result.stdout + result.stderr
            report = json.loads((tmp_path / 'sync-benchmark.json').read_text())
            assert mode(report)['Synchronous standby policy'] == 'quorum (ANY)'
            assert mode(report)['Current quorum senders'] == '1'
            assert rows(report, 'replication_slots')[0]['active'] is True
            assert report['benchmark_runs'][0]['metrics']['tps'] > 0
            assert 'Replication' in (tmp_path / 'sync-benchmark.html').read_text()
        finally:
            if standby:
                standby.remove(force=True, v=True)
            client.close()
