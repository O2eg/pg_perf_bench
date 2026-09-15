"""Temporary primary settings with durable recovery information.

Only fsync is changed at server scope, on the directly connected primary.
Commit policy belongs to loader connections and never changes Patroni's DCS.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from pg_perf_bench.const import ConnectionType
from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.initialization import LoadOptions, loader_connection, prepare_database

_LOCK = (1346847301, 1229867348)
_STATE_DIR = Path.home() / '.cache/pg_perf_bench/initialization'


class InitializationSettings:
    def __init__(
        self,
        logger,
        db_conf,
        options: LoadOptions,
        connection_type,
        connection,
        *,
        state_dir: Path | None = None,
        control_database: str = 'postgres',
    ):
        self.logger = logger
        self.db_conf = db_conf
        self.options = options
        self.connection_type = connection_type
        self.connection = connection
        self.state_dir = state_dir or _STATE_DIR
        self.control_database = control_database
        self.db = None
        self.session = None
        self.lock_acquired = False
        self.state_path = None
        self.identity = None
        self.original = None
        self.changed = False
        self.replicas: Counter[tuple[str, ...]] = Counter()

    async def _sync(self):
        if self.connection_type == ConnectionType.MANAGED or self.connection is None:
            raise ConfigurationError(
                'Restoring fsync requires database-host access; use --init-fsync keep'
            )
        # Execute on the selected database host (also for remote Docker daemons).
        await self.connection.run_command('sync', check=True, timeout=self.options.timeout)

    async def _identity(self):
        identity = dict(
            await self.db.fetchrow(
                'SELECT system_identifier::text AS system_identifier, '
                'inet_server_addr()::text AS address, inet_server_port() AS port, '
                "pg_read_file('postmaster.pid') AS postmaster_pid, "
                "current_setting('data_directory') AS data_directory FROM pg_control_system()"
            )
        )
        postmaster_pid = identity.pop('postmaster_pid')
        if self.connection_type != ConnectionType.MANAGED and self.connection is not None:
            # A physical replica shares system_identifier and can have the same
            # PGDATA path. Identify the host and directory, not the SQL endpoint.
            host = (
                await self.connection.run_command(
                    "uname -n && stat -L -c '%d:%i' -- "
                    + shlex.quote(identity['data_directory'])
                    + ' && cat -- '
                    + shlex.quote(identity['data_directory'] + '/postmaster.pid'),
                    check=True,
                    timeout=self.options.timeout,
                )
            ).splitlines()
            # A SQL VIP may move while SSH/Docker still targets the old host.
            # Confirm that both transports see the same running postmaster.
            if len(host) < 3 or host[2:] != postmaster_pid.splitlines():
                raise ConfigurationError(
                    'SQL connection and database-host access target different servers'
                )
            identity['host'] = '\n'.join(host[:2])
        return identity

    @staticmethod
    def _member_identity(identity):
        if 'host' not in identity:
            # Old journals cannot identify a member independently of its address.
            return identity
        return {key: identity[key] for key in ('system_identifier', 'data_directory', 'host')}

    def _recovery_journal(self):
        matches = []
        for path in self.state_dir.glob('*.json'):
            state = json.loads(path.read_text())
            original = state.get('identity', {})
            if original.get('system_identifier') != self.identity['system_identifier']:
                continue
            current = self.identity
            if 'host' not in original:
                current = {key: value for key, value in current.items() if key != 'host'}
            if self._member_identity(original) != self._member_identity(current):
                raise ConfigurationError(
                    'Pending fsync recovery requires the original database host '
                    '(and the original SQL address for an older journal). '
                    f'Recover it before starting another benchmark. Journal: {path}'
                )
            matches.append((path, state))
        if len(matches) > 1:
            raise ConfigurationError('Multiple fsync recovery journals match this server')
        return matches[0] if matches else None

    async def _auto_value(self):
        return await self.db.fetchval(
            "SELECT setting FROM pg_file_settings WHERE name='fsync' "
            "AND sourcefile = current_setting('data_directory') || '/postgresql.auto.conf' "
            'ORDER BY seqno DESC LIMIT 1'
        )

    def _save(self, state):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A completed fsync of the journal precedes disabling server fsync.
        with self.state_path.open('x', encoding='utf-8') as stream:
            os.chmod(self.state_path, 0o600)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    async def _reload(self, expected):
        await self.db.fetchval('SELECT pg_reload_conf()')
        deadline = time.monotonic() + min(self.options.timeout, 30)
        while time.monotonic() < deadline:
            if await self.db.fetchval('SHOW fsync') == expected:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f'fsync did not become {expected!r}; initialization cannot continue')

    @staticmethod
    def recovery_pending():
        return any(_STATE_DIR.glob('*.json'))

    async def open(self, *, recover_only=False):
        await self._connect()
        try:
            if await self.db.fetchval('SELECT pg_is_in_recovery()'):
                raise ConfigurationError('Fast initialization requires a primary PostgreSQL server')
            if not await self.db.fetchval('SELECT pg_try_advisory_lock($1, $2)', *_LOCK):
                raise ConfigurationError(
                    "Another initialization is changing this primary's settings"
                )
            self.lock_acquired = True
            is_superuser = await self.db.fetchval("SELECT current_setting('is_superuser')::boolean")
            pending_recovery = any(self.state_dir.glob('*.json'))
            if pending_recovery and not is_superuser:
                # An unprivileged role cannot reliably identify the journal's
                # server. Do not silently skip recovery, even with fsync=keep.
                raise ConfigurationError(
                    'Pending fsync recovery requires a PostgreSQL superuser to check and restore '
                    'the original server before starting another benchmark. '
                    f'Journal directory: {self.state_dir}'
                )
            if self.options.fsync == 'off':
                if self.connection_type == ConnectionType.MANAGED or self.connection is None:
                    raise ConfigurationError(
                        'fsync=off requires database-host access; use --init-fsync keep'
                    )
                if not is_superuser:
                    raise ConfigurationError(
                        'fsync=off requires a PostgreSQL superuser; use --init-fsync keep'
                    )
            if is_superuser:
                if self.options.fsync == 'off' or pending_recovery:
                    self.identity = await self._identity()
                    key = hashlib.sha256(
                        json.dumps(self._member_identity(self.identity), sort_keys=True).encode()
                    ).hexdigest()[:24]
                    self.state_path = self.state_dir / (key + '.json')
                    journal = self._recovery_journal()
                else:
                    journal = None
                if journal is not None:
                    self.state_path, state = journal
                    if self.connection_type == ConnectionType.MANAGED or self.connection is None:
                        raise ConfigurationError('Restoring fsync requires database-host access')
                    self.logger.warning(
                        'Recovering fsync from an interrupted initialization: %s', self.state_path
                    )
                    self.original = state
                    self.changed = True
                    await self.restore()
                if self.options.fsync == 'off':
                    source = await self.db.fetchval(
                        "SELECT source FROM pg_settings WHERE name='fsync'"
                    )
                    if source == 'command line':
                        raise ConfigurationError(
                            'Command-line fsync cannot be overridden; use --init-fsync keep'
                        )
                    # Confirm host access before altering any setting or resetting the database.
                    await self._sync()
            if not recover_only:
                try:
                    self.replicas = await self._replica_keys()
                    # Check WAL-function permissions before resetting/loading, not at the barrier.
                    await self.db.fetchval('SELECT pg_current_wal_insert_lsn()::text')
                    await self.db.fetchval('SHOW fsync')
                except asyncpg.InsufficientPrivilegeError as exc:
                    raise ConfigurationError(
                        'Replica readiness requires access to replication statistics and WAL '
                        'functions; grant monitoring privileges using the provider controls '
                        f'or PostgreSQL roles before loading data: {exc}'
                    ) from exc
        except BaseException:
            await self._disconnect()
            raise

    async def _connect(self):
        self.session = loader_connection(
            {**self.db_conf, 'database': self.control_database}, self.options
        )
        self.db = await self.session.__aenter__()

    async def _disconnect(self):
        try:
            if self.lock_acquired and self.db is not None and not self.db.is_closed():
                # Closing a client need not close its pooled PostgreSQL backend.
                await self.db.fetchval('SELECT pg_advisory_unlock($1, $2)', *_LOCK)
        finally:
            self.lock_acquired = False
            try:
                if self.session is not None:
                    await self.session.__aexit__(None, None, None)
            finally:
                self.db = None
                self.session = None

    async def disable(self):
        if self.options.fsync == 'keep':
            return
        self.original = {
            'identity': self.identity,
            'fsync': await self.db.fetchval('SHOW fsync'),
            'auto_value': await self._auto_value(),
        }
        self._save(self.original)
        self.changed = True
        await self.db.execute('ALTER SYSTEM SET fsync = off')
        await self._reload('off')
        self.logger.info(
            'Initialization: fsync=off on the selected primary; recovery journal: %s.',
            self.state_path,
        )

    async def restore(self):
        if not self.changed:
            return
        if self.db.is_closed():
            await self._disconnect()
            await self._connect()
            if not await self.db.fetchval('SELECT pg_try_advisory_lock($1, $2)', *_LOCK):
                raise RuntimeError('Cannot recover fsync: another initialization holds the lock')
            self.lock_acquired = True
        if await self.db.fetchval('SELECT pg_is_in_recovery()') or self._member_identity(
            await self._identity()
        ) != self._member_identity(self.identity):
            raise RuntimeError(
                'Primary changed; fsync recovery requires the original host. '
                f'Journal: {self.state_path}'
            )
        value = self.original['auto_value']
        if value is None:
            await self.db.execute('ALTER SYSTEM RESET fsync')
        else:
            # Only a boolean from pg_file_settings is interpolated, never arbitrary SQL.
            if str(value).lower() not in {'on', 'off', 'true', 'false', '1', '0', 'yes', 'no'}:
                raise ConfigurationError('Invalid original fsync value in recovery journal')
            await self.db.execute('ALTER SYSTEM SET fsync = ' + ("'" + str(value) + "'"))
        await self._reload(self.original['fsync'])
        # CHECKPOINT flushes shared buffers. sync also covers old kernel dirty
        # buffers whose fsync requests were skipped while fsync was disabled.
        await self.db.execute('CHECKPOINT')
        await self._sync()
        self.state_path.unlink()
        self.changed = False
        self.logger.info(
            'Initialization: fsync restored to %s and host files synchronized.',
            self.original['fsync'],
        )

    async def _replica_keys(self, target=None):
        replay_filter = (
            " AND r.state='streaming' AND r.replay_lsn >= $1::text::pg_lsn"
            if target is not None
            else ''
        )
        rows = await self.db.fetch(
            "SELECT r.application_name, r.state, coalesce(r.client_addr::text, '') AS address, "
            's.slot_name '
            'FROM pg_stat_replication r LEFT JOIN pg_replication_slots s ON s.active_pid=r.pid '
            "WHERE s.slot_type IS DISTINCT FROM 'logical' "
            "AND (r.state IS NULL OR r.state <> 'backup')" + replay_filter,
            *((target,) if target is not None else ()),
        )
        if any(row['state'] is None for row in rows):
            raise ConfigurationError(
                'Replica readiness requires visibility of pg_stat_replication; '
                'grant pg_read_all_stats/pg_monitor or equivalent provider monitoring privileges '
                'to the benchmark role before loading data'
            )
        # Physical slots survive reconnections from a new address. Without a
        # slot, retain counts for replicas sharing an application name behind NAT.
        return Counter(
            ('slot', row['slot_name'])
            if row['slot_name'] is not None
            else ('connection', row['application_name'], row['address'])
            for row in rows
        )

    async def wait_for_replicas(self) -> dict[str, Any]:
        target = await self.db.fetchval('SELECT pg_current_wal_insert_lsn()::text')
        required = self.replicas | await self._replica_keys()
        started = time.monotonic()
        while required:
            caught_up = await self._replica_keys(target)
            if all(caught_up[key] >= count for key, count in required.items()):
                break
            if time.monotonic() - started > self.options.timeout:
                raise RuntimeError('Replicas did not replay initialization WAL before the timeout')
            await asyncio.sleep(0.2)
        return {
            'target_lsn': target,
            'replicas': sum(required.values()),
            'elapsed_seconds': time.monotonic() - started,
        }

    async def close(self):
        try:
            await self.restore()
        finally:
            await self._disconnect()


async def initialize_database(
    logger,
    plan,
    db_conf,
    options,
    connection_type,
    connection,
    *,
    required_replicas=None,
    settings=None,
):
    owns_settings = settings is None
    if owns_settings:
        settings = InitializationSettings(logger, db_conf, options, connection_type, connection)
        await settings.open()
    try:
        settings.replicas |= required_replicas or Counter()
        await settings.disable()
        evidence = await prepare_database(logger, plan, db_conf, options)
        started = time.monotonic()
        await settings.restore()
        evidence['phases'].append(
            {'name': 'restore_settings_and_sync', 'elapsed_seconds': time.monotonic() - started}
        )
        evidence['replication_barrier'] = await settings.wait_for_replicas()
        evidence['fsync_after'] = await settings.db.fetchval('SHOW fsync')
        evidence['finished_at'] = datetime.now(timezone.utc).isoformat()
        return evidence
    finally:
        if owns_settings:
            await close_initialization_settings(settings)


async def close_initialization_settings(settings):
    # Restore fsync/release the session lock even when the caller is cancelled.
    cleanup = asyncio.create_task(settings.close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise
