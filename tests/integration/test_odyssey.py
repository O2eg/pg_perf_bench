"""Real session-pool regression coverage; install the pinned Odyssey image first."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import asyncpg
import docker
import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import LoadOptions, LoadPlan, LoadTask, prepare_database
from pg_perf_bench.initialization_settings import (
    InitializationSettings,
    close_initialization_settings,
)
from pg_perf_bench.storage import collect_storage_snapshot
from tests.integration.test_managed_schema import PASSWORD, identity, restrict_server
from tests.integration.test_replication_report import postgres

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_INIT_INTEGRATION') != '1',
        reason='set PG_PERF_BENCH_INIT_INTEGRATION=1 for disposable pool tests',
    ),
]
LOGGER = logging.getLogger(__name__)
IMAGE = 'ghcr.io/yandex/odyssey:1.5.0'


@contextmanager
def odyssey(primary, limited, tmp_path, *, smart='yes', discard='no'):
    client = docker.from_env()
    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        client.close()
        pytest.skip(f'Integration image is not installed: {IMAGE}')
    primary.reload()
    address = primary.attrs['NetworkSettings']['Networks']['bridge']['IPAddress']
    config = tmp_path / 'odyssey.conf'
    config.write_text(f'''daemonize no
log_to_stdout yes
log_format "%p %t %l [%i %s] (%c) %m"
workers 2
resolvers 1
smart_search_path_enquoting {smart}
listen {{ host "*" port 6432 tls "disable" }}
storage "primary" {{ type "remote" host "{address}" port 5432 }}
database "benchdb" {{ user "bench_owner" {{
 authentication "clear_text"
 password "{PASSWORD}"
 storage "primary"
 storage_db "benchdb"
 storage_user "bench_owner"
 storage_password "{PASSWORD}"
 pool "session"
 pool_size 32
 pool_timeout 5000
 pool_discard {discard}
 pool_cancel yes
 pool_rollback yes
 pool_reserve_prepared_statement yes
}} }}
''')
    proxy = client.containers.run(
        IMAGE,
        name='pg-perf-odyssey-' + uuid.uuid4().hex[:10],
        volumes={str(config): {'bind': '/etc/odyssey/odyssey.conf', 'mode': 'ro'}},
        ports={'6432/tcp': ('127.0.0.1', None)},
        detach=True,
    )
    try:
        proxy.reload()
        assert proxy.status == 'running', proxy.logs().decode()
        port = int(proxy.attrs['NetworkSettings']['Ports']['6432/tcp'][0]['HostPort'])
        remote = {**limited, 'port': port}
        for _ in range(100):
            try:

                async def ready():
                    db = await asyncpg.connect(**remote, timeout=1)
                    await db.close()

                asyncio.run(ready())
                break
            except (OSError, asyncpg.PostgresError):
                time.sleep(0.1)
        else:
            pytest.fail(proxy.logs().decode())
        yield remote
    finally:
        (tmp_path / 'odyssey.log').write_bytes(proxy.logs())
        proxy.remove(force=True, v=True)
        client.close()


@pytest.mark.parametrize(
    ('smart', 'discard', 'message'),
    [('no', 'yes', 'smart_search_path_enquoting=yes'), ('yes', 'no', 'pool_discard=yes')],
)
def test_incompatible_pool_rejected_before_schema_reset(tmp_path, smart, discard, message):
    with postgres(18) as (primary, conf):
        limited = asyncio.run(restrict_server(conf))
        with odyssey(primary, limited, tmp_path, smart=smart, discard=discard) as remote:

            async def scenario():
                db = await asyncpg.connect(**limited)
                try:
                    await db.execute(
                        'CREATE SCHEMA pagila; CREATE TABLE pagila.keep_me AS SELECT 42 id'
                    )
                    original = await db.fetchval("SELECT 'pagila.keep_me'::regclass::oid")
                    with patch(
                        'pg_perf_bench.benchmark.load_plan',
                        return_value=LoadPlan(('pagila',), '', (), ()),
                    ):
                        with pytest.raises(ConfigurationError, match=message):
                            await BenchmarkRunner.run_benchmark_iterations(
                                LOGGER,
                                [['init', 'unused']],
                                'managed',
                                None,
                                remote,
                                {
                                    'init_mode': 'fast',
                                    'init_entrypoint': 'unused.py',
                                    'workload_path': str(tmp_path),
                                    'reset_mode': 'schema',
                                    'init_fsync': 'keep',
                                    'allow_database_reset': True,
                                },
                            )
                    assert await db.fetchval("SELECT 'pagila.keep_me'::regclass::oid") == original
                    assert await db.fetchval('SELECT id FROM pagila.keep_me') == 42
                finally:
                    await db.close()

            asyncio.run(scenario())


@pytest.mark.parametrize('policy', ['keep', 'off', 'local'])
def test_loader_sql_settings_and_cleanup_without_pool_discard(tmp_path, policy):
    with postgres(18) as (primary, conf):
        limited = asyncio.run(restrict_server(conf))

        async def set_default():
            db = await asyncpg.connect(**limited)
            try:
                await db.execute('ALTER DATABASE benchdb SET synchronous_commit=remote_apply')
            finally:
                await db.close()

        asyncio.run(set_default())
        # Even the old Odyssey search_path handling must not affect SQL loader sessions.
        with odyssey(primary, limited, tmp_path, smart='no') as remote:

            async def scenario():
                observed = 'remote_apply' if policy == 'keep' else policy
                plan = LoadPlan(
                    ('first', 'second'),
                    'CREATE SCHEMA first; CREATE SCHEMA second; '
                    'CREATE TABLE second.data (id bigint, policy text); '
                    'CREATE TABLE first.setup (policy text);',
                    (
                        LoadTask(
                            'rows',
                            "INSERT INTO data SELECT g, current_setting('synchronous_commit') "
                            'FROM generate_series($1::bigint, $2::bigint) g',
                            count=7,
                        ),
                    ),
                    (LoadTask('index', 'CREATE UNIQUE INDEX data_pk ON data (id)'),),
                    prepare_sql="INSERT INTO setup VALUES (current_setting('synchronous_commit'))",
                    finalize_sql="INSERT INTO setup VALUES (current_setting('synchronous_commit'))",
                )
                options = LoadOptions(fsync='keep', synchronous_commit=policy, batch_rows=2)
                await prepare_database(LOGGER, plan, remote, options)
                await collect_storage_snapshot(LOGGER, remote)
                db = await asyncpg.connect(**limited)
                try:
                    assert (
                        await db.fetchval(
                            'SELECT count(*) FROM second.data WHERE policy=$1', observed
                        )
                        == 7
                    )
                    assert (
                        await db.fetchval(
                            'SELECT count(*) FROM first.setup WHERE policy=$1', observed
                        )
                        == 2
                    )
                    assert await db.fetchval(
                        "SELECT bool_and(relpersistence='p') FROM pg_class "
                        "WHERE oid IN ('second.data'::regclass, 'first.setup'::regclass)"
                    )
                finally:
                    await db.close()
                # Occupy the whole pool to check all reused backends, including every worker.
                sessions = []
                try:
                    for _ in range(8):
                        db = await asyncpg.connect(**remote)
                        sessions.append(db)
                        assert await db.fetchval('SHOW synchronous_commit') == 'remote_apply'
                        assert await db.fetchval('SHOW search_path') == 'pg_catalog'
                        assert await db.fetchval('SHOW default_transaction_read_only') == 'off'
                        assert await db.fetchval('SHOW statement_timeout') == '0'
                        assert await db.fetchval('SHOW lock_timeout') == '0'
                finally:
                    await asyncio.gather(*(db.close() for db in sessions))

            asyncio.run(scenario())


@pytest.mark.parametrize('ending', ['success', 'preflight_error', 'cancel'])
def test_advisory_lock_released_before_return_to_pool(tmp_path, ending):
    with postgres(18) as (primary, conf):
        limited = asyncio.run(restrict_server(conf))
        with odyssey(primary, limited, tmp_path) as remote:

            async def scenario():
                guard = InitializationSettings(
                    LOGGER,
                    remote,
                    LoadOptions(fsync='keep', synchronous_commit='off'),
                    'managed',
                    None,
                    control_database='benchdb',
                    state_dir=tmp_path / 'state',
                )
                if ending == 'preflight_error':
                    with patch.object(
                        guard,
                        '_replica_keys',
                        side_effect=asyncpg.InsufficientPrivilegeError('hidden'),
                    ):
                        with pytest.raises(ConfigurationError, match='before loading data'):
                            await guard.open()
                elif ending == 'cancel':
                    started = asyncio.Event()

                    async def run():
                        await guard.open()
                        try:
                            started.set()
                            await asyncio.Event().wait()
                        finally:
                            await close_initialization_settings(guard)

                    task = asyncio.create_task(run())
                    await started.wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await guard.open()
                    await close_initialization_settings(guard)
                observer = await asyncpg.connect(**conf)
                held = await asyncpg.connect(**remote)
                try:
                    assert (
                        await observer.fetchval(
                            "SELECT count(*) FROM pg_locks WHERE locktype='advisory'"
                        )
                        == 0
                    )
                    # The old backend is occupied by another client; the next guard needs
                    # an independent backend and must not see a stale competing lock.
                    await held.fetchval('SELECT pg_backend_pid()')
                    assert await held.fetchval('SHOW synchronous_commit') == 'on'
                    other = InitializationSettings(
                        LOGGER,
                        remote,
                        LoadOptions(fsync='keep'),
                        'managed',
                        None,
                        control_database='benchdb',
                        state_dir=tmp_path / 'state',
                    )
                    await other.open()
                    await other.close()
                finally:
                    await held.close()
                    await observer.close()

            asyncio.run(scenario())


@pytest.mark.parametrize(
    ('profile', 'scale'), [('pagila', '1.15'), ('pagila-htap', '1.15'), ('imdb', '0.03')]
)
def test_full_cli_repeated_through_session_pool(tmp_path, profile, scale):
    with postgres(18) as (primary, conf):
        limited = asyncio.run(restrict_server(conf))
        original = asyncio.run(identity(conf))
        with odyssey(primary, limited, tmp_path, discard='yes') as remote:
            for attempt, policy in enumerate(('keep', 'off')):
                name = f'{profile}-{attempt}'
                result = subprocess.run(
                    [
                        sys.executable,
                        '-m',
                        'pg_perf_bench',
                        'benchmark',
                        '--managed',
                        '--host',
                        remote['host'],
                        '--port',
                        str(remote['port']),
                        '--user',
                        remote['user'],
                        '--database',
                        remote['database'],
                        '--allow-database-reset',
                        '--reset-mode',
                        'schema',
                        '--init-fsync',
                        'keep',
                        '--init-synchronous-commit',
                        policy,
                        '--workload-profile',
                        profile,
                        '--workload-scale',
                        scale,
                        '--pgbench-clients',
                        '1,2',
                        '--workload-duration-seconds',
                        '1',
                        '--command-timeout',
                        '30',
                        '--output-dir',
                        str(tmp_path),
                        '--report-name',
                        name,
                        '--log-dir',
                        str(tmp_path / 'logs'),
                    ],
                    env={**os.environ, 'PGPASSWORD': PASSWORD, 'PGSSLMODE': 'disable'},
                    text=True,
                    capture_output=True,
                    timeout=120,
                )
                (tmp_path / f'{name}.log').write_text(result.stdout + result.stderr)
                assert result.returncode in (0, 5), result.stdout + result.stderr
                report = json.loads((tmp_path / f'{name}.json').read_text())
                assert len(report['benchmark_runs']) == 2
                for run in report['benchmark_runs']:
                    assert run['metrics']['tps'] > 0
                    assert run['metrics']['failed_transactions_percent'] == 0
                    assert run['initialization']['fsync_after'] == 'on'
                assert asyncio.run(identity(conf)) == original
        logs = primary.logs().decode()
        assert 'bench_owner@postgres' not in logs
        assert 'bench_owner@template1' not in logs
