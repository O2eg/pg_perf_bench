"""Database and relation sizes at each benchmark iteration's workload boundaries."""

from copy import deepcopy
from datetime import datetime, timezone

import asyncpg

from pg_perf_bench.report.commands import run_sql_command
from pg_perf_bench.session_settings import close_diagnostic_connection


def _connection_kwargs(db_conf):
    kwargs = {key: value for key, value in db_conf.items() if key != 'connect_timeout'}
    kwargs['timeout'] = float(db_conf.get('connect_timeout', 5.0))
    return kwargs


async def vacuum_analyze(logger, db_conf, timeout):
    """Finish preparation outside a transaction and outside the measured workload."""
    logger.info('Running VACUUM ANALYZE before the workload size snapshot.')
    db = await asyncpg.connect(**_connection_kwargs(db_conf))
    try:
        await db.execute('VACUUM ANALYZE', timeout=timeout)
    finally:
        await db.close()


def _items():
    return {
        'database_sizes': {
            'header': 'Database sizes (all databases)',
            'description': 'Physical sizes across all tablespaces, including template databases. '
            'is_workload_database marks the benchmark target. WAL and shared cluster files '
            'are excluded. Unavailable sizes remain NULL with a per-database reason.',
            'item_type': 'table',
            'state': 'expanded',
        },
        'table_sizes': {
            'header': 'Top 100 tables by total size',
            'description': 'Stored user tables and materialized views in the workload database, '
            'ordered by measured total size. Leaf partitions appear separately; storage-free '
            'partitioned parents are omitted. table_size_bytes includes TOAST and auxiliary '
            'forks; total_size_bytes also includes table indexes. toast_size_bytes is already '
            'included in table_size_bytes and must not be added again.',
            'item_type': 'table',
            'state': 'expanded',
            'sql_command_file': 'storage_tables.sql',
            'empty_message': 'No stored user tables or materialized views '
            'in the workload database.',
        },
        'index_sizes': {
            'header': 'Top 100 indexes by size',
            'description': 'Indexes of stored user tables and materialized views in the workload '
            'database, ordered by measured size including all forks. Physical indexes of '
            'partitions appear separately. System and TOAST indexes are excluded; TOAST storage '
            'is included in the table item.',
            'item_type': 'table',
            'state': 'expanded',
            'sql_command_file': 'storage_indexes.sql',
            'empty_message': 'No user indexes in the workload database.',
        },
    }


def _failed_item(item, exc):
    reason = str(exc) or type(exc).__name__
    item.update(item_type='plain_text', collection_status='error', reason=reason, data=reason)


async def _database_sizes(db, item):
    # One inaccessible database must not discard the other databases' sizes.
    databases = await db.fetch(
        'SELECT oid::bigint AS database_oid, datname::text AS database_name, '
        'datname = current_database() AS is_workload_database, '
        'datistemplate AS is_template, datallowconn AS allows_connections '
        'FROM pg_catalog.pg_database ORDER BY datname'
    )
    rows = []
    for database in databases:
        row = dict(database)
        row.update(database_size_bytes=None, database_size=None, collection_status='ok', reason='')
        try:
            size = await db.fetchrow(
                'SELECT size AS database_size_bytes, pg_catalog.pg_size_pretty(size) '
                'AS database_size FROM (SELECT pg_catalog.pg_database_size($1::oid) AS size) s',
                database['database_oid'],
            )
            row.update(dict(size))
            if row['database_size_bytes'] is None:
                row.update(collection_status='error', reason='Database size is unavailable')
        except Exception as exc:
            row.update(collection_status='error', reason=str(exc) or type(exc).__name__)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            row['database_size_bytes'] is None,
            -(row['database_size_bytes'] or 0),
            row['database_name'],
        )
    )
    item['theader'] = list(rows[0]) if rows else []
    item['data'] = [[row[key] for key in item['theader']] for row in rows]
    failed = sum(row['collection_status'] != 'ok' for row in rows)
    item['collection_status'] = 'partial' if failed else ('ok' if rows else 'empty')
    if failed:
        item['reason'] = f'Size unavailable for {failed} database(s); see per-database reasons.'


async def collect_storage_snapshot(logger, db_conf):
    snapshot = {'started_at': datetime.now(timezone.utc).isoformat(), 'reports': _items()}
    kwargs = _connection_kwargs(db_conf)
    kwargs['server_settings'] = {
        **kwargs.get('server_settings', {}),
        'default_transaction_read_only': 'on',
        'statement_timeout': '10000',
        'lock_timeout': '1000',
    }
    kwargs['command_timeout'] = 10.0
    try:
        db = await asyncpg.connect(**kwargs)
    except Exception as exc:
        for item in snapshot['reports'].values():
            _failed_item(item, exc)
    else:
        try:
            try:
                await _database_sizes(db, snapshot['reports']['database_sizes'])
            except Exception as exc:
                _failed_item(snapshot['reports']['database_sizes'], exc)
            for name in ('table_sizes', 'index_sizes'):
                await run_sql_command(logger, db, snapshot['reports'][name])
        finally:
            await close_diagnostic_connection(db)
    # These are captured results, never commands for the final monitoring pass to rerun.
    for item in snapshot['reports'].values():
        item.pop('sql_command_file', None)
    snapshot['completed_at'] = datetime.now(timezone.utc).isoformat()
    return snapshot


def build_storage_section(benchmark_runs):
    section = {
        'header': 'Storage sizes before and after workload',
        'description': 'A separate pair of snapshots for every iteration. Before workload is '
        'collected after initialization and VACUUM ANALYZE; After workload is collected after '
        'pgbench and OS sampling finish, before the next database reset. Each snapshot is '
        'a sequence of size measurements, not an atomic cluster-wide snapshot.',
        'state': 'expanded',
        'reports': {},
    }
    for run in benchmark_runs:
        iteration = run['iteration']
        index = iteration['index']
        label = f'Iteration {index}: {iteration["parameter"]}={iteration["value"]}'
        for phase in ('before_workload', 'after_workload'):
            snapshot = run.get('storage', {}).get(phase)
            if snapshot is None:
                continue
            for name, source in snapshot['reports'].items():
                item = deepcopy(source)
                item['header'] = (
                    f'{label} | {phase.replace("_", " ").capitalize()} | {item["header"]}'
                )
                item['description'] += (
                    f' Collection interval: {snapshot["started_at"]} — {snapshot["completed_at"]}.'
                )
                section['reports'][f'iteration_{index}_{phase}_{name}'] = item
    return section
