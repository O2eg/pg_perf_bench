"""Opt-in physical size checks and workload boundary evidence on real PostgreSQL."""

import asyncio
import json
import os
import shlex
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import asyncpg
import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.report.processing import save_report
from pg_perf_bench.storage import build_storage_section, collect_storage_snapshot, vacuum_analyze
from tests.integration.test_replication_report import postgres

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_STORAGE_INTEGRATION') != '1',
        reason='set PG_PERF_BENCH_STORAGE_INTEGRATION=1 to start PostgreSQL containers',
    ),
]


def rows(snapshot, name):
    item = snapshot['reports'][name]
    assert item['collection_status'] in {'ok', 'partial', 'empty'}, item
    if item['collection_status'] == 'empty':
        return []
    return [dict(zip(item['theader'], row, strict=True)) for row in item['data']]


@pytest.mark.parametrize('major', range(10, 19))
def test_all_databases_real_top100_growth_and_permissions(major, tmp_path):
    with postgres(major) as (_container, connection):

        async def scenario():
            db = await asyncpg.connect(**connection)
            try:
                await db.execute('CREATE DATABASE other_db')
                await db.execute('CREATE DATABASE closed_db ALLOW_CONNECTIONS false')
                await db.execute('REVOKE CONNECT ON DATABASE other_db FROM PUBLIC')
                await db.execute("CREATE ROLE size_reader LOGIN PASSWORD 'size-test'")
                empty = await collect_storage_snapshot(MagicMock(), connection)
                assert not rows(empty, 'table_sizes')
                assert not rows(empty, 'index_sizes')
                await db.execute("""
                    DO $$ BEGIN
                        FOR i IN 1..205 LOOP
                            EXECUTE format('CREATE TABLE filler_%s (id int PRIMARY KEY)', i);
                            EXECUTE format('INSERT INTO filler_%s SELECT generate_series(1,10)', i);
                        END LOOP;
                    END $$;
                    CREATE TABLE grown (id int PRIMARY KEY, payload text)
                        WITH (autovacuum_enabled=false);
                    ALTER TABLE grown ALTER COLUMN payload SET STORAGE EXTERNAL;
                    CREATE TABLE parent (id int) PARTITION BY RANGE (id);
                    CREATE TABLE leaf PARTITION OF parent FOR VALUES FROM (0) TO (100);
                    CREATE INDEX leaf_idx ON leaf (id);
                    INSERT INTO leaf VALUES (1);
                    CREATE MATERIALIZED VIEW materialized AS SELECT generate_series(1, 5) AS id;
                    CREATE INDEX materialized_idx ON materialized (id);
                """)
                await vacuum_analyze(MagicMock(), connection, 30)
                before = await collect_storage_snapshot(MagicMock(), connection)
                assert len(rows(before, 'table_sizes')) == 100
                assert len(rows(before, 'index_sizes')) == 100
                # This heap had zero catalog pages at the before snapshot. It must still
                # lead both top-100 lists after growth without ANALYZE.
                await db.execute("""
                    INSERT INTO grown
                    SELECT g, repeat(md5(g::text), 160) FROM generate_series(1, 5000) g
                """)
                assert (
                    await db.fetchval("SELECT relpages FROM pg_class WHERE oid='grown'::regclass")
                    == 0
                )
                after = await collect_storage_snapshot(MagicMock(), connection)
                table_rows = rows(after, 'table_sizes')
                index_rows = rows(after, 'index_sizes')
                assert table_rows[0]['table_name'] == 'grown'
                assert index_rows[0]['index_name'] == 'grown_pkey'
                assert len(table_rows) == len(index_rows) == 100
                assert table_rows[0]['toast_size_bytes'] > 0
                for item_rows, size_column in (
                    (table_rows, 'total_size_bytes'),
                    (index_rows, 'index_size_bytes'),
                ):
                    assert [row[size_column] for row in item_rows] == sorted(
                        (row[size_column] for row in item_rows), reverse=True
                    )
                    assert all(isinstance(row[size_column], int) for row in item_rows)
                for row in table_rows:
                    assert row['total_size_bytes'] == await db.fetchval(
                        'SELECT pg_total_relation_size($1::oid)', row['table_oid']
                    )
                    assert (
                        row['total_size_bytes']
                        == row['table_size_bytes'] + row['indexes_size_bytes']
                    )
                    assert row['schema_name'] == 'public'
                    assert row['table_name'] != 'parent'
                for row in index_rows:
                    assert row['index_size_bytes'] == await db.fetchval(
                        'SELECT pg_table_size($1::oid)', row['index_oid']
                    )
                databases = {row['database_name']: row for row in rows(after, 'database_sizes')}
                assert set(databases) == {
                    'benchdb',
                    'postgres',
                    'template0',
                    'template1',
                    'other_db',
                    'closed_db',
                }
                assert [
                    row['database_name']
                    for row in databases.values()
                    if row['is_workload_database']
                ] == ['benchdb']
                assert not databases['closed_db']['allows_connections']
                before_db = {row['database_name']: row for row in rows(before, 'database_sizes')}
                assert (
                    databases['benchdb']['database_size_bytes']
                    > before_db['benchdb']['database_size_bytes']
                )
                restricted = await collect_storage_snapshot(
                    MagicMock(), {**connection, 'user': 'size_reader', 'password': 'size-test'}
                )
                limited = {row['database_name']: row for row in rows(restricted, 'database_sizes')}
                assert set(limited) == set(databases)
                assert limited['other_db']['database_size_bytes'] is None
                assert limited['other_db']['collection_status'] == 'error'
                assert limited['benchdb']['database_size_bytes'] > 0
                assert len(rows(restricted, 'table_sizes')) == 100
                # Remove fillers to inspect stored partitions/materialized views explicitly.
                await db.execute(
                    'DO $$ BEGIN FOR i IN 1..205 LOOP '
                    "EXECUTE format('DROP TABLE filler_%s', i); END LOOP; END $$"
                )
                smaller = await collect_storage_snapshot(MagicMock(), connection)
                assert {row['table_name'] for row in rows(smaller, 'table_sizes')} == {
                    'grown',
                    'leaf',
                    'materialized',
                }
                assert {row['index_name'] for row in rows(smaller, 'index_sizes')} == {
                    'grown_pkey',
                    'leaf_idx',
                    'materialized_idx',
                }
                run = {
                    'iteration': {'index': 1, 'parameter': 'clients', 'value': 1},
                    'storage': {'before_workload': before, 'after_workload': after},
                }
                report = {
                    'report_name': f'storage-pg{major}',
                    'benchmark_runs': [run],
                    'sections': {'storage': build_storage_section([run])},
                }
                save_report(MagicMock(), report, tmp_path)
                json.dumps(report, allow_nan=False)
                html = (tmp_path / f'storage-pg{major}.html').read_text()
                assert 'Before workload' in html and 'After workload' in html
                assert 'other_db' in html and 'grown_pkey' in html
            finally:
                await db.close()

        asyncio.run(scenario())


@pytest.mark.parametrize('connection_type', ['managed', 'docker'])
def test_actual_pgbench_initialization_vacuum_and_size_growth(tmp_path, connection_type):
    pgbench = Path('/usr/lib/postgresql/18/bin/pgbench')
    psql = Path('/usr/lib/postgresql/18/bin/psql')
    if not pgbench.is_file() or not psql.is_file():
        pytest.skip('PostgreSQL 18 client binaries are required')
    init = tmp_path / 'init.sql'
    init.write_text("""
        CREATE TABLE growth (id bigserial PRIMARY KEY, payload text);
        ALTER TABLE growth ALTER COLUMN payload SET STORAGE EXTERNAL;
        INSERT INTO growth (payload) SELECT repeat('a', 5000) FROM generate_series(1, 10);
    """)
    script = tmp_path / 'growth.sql'
    # The workload itself proves that VACUUM and ANALYZE finished before pgbench started.
    script.write_text("""
        SELECT 1 / (EXISTS (SELECT 1 FROM pg_stat_user_tables
            WHERE relname='growth' AND last_vacuum IS NOT NULL
            AND last_analyze IS NOT NULL))::int;
        INSERT INTO growth (payload) VALUES (repeat('b', 5000));
    """)
    with postgres(18) as (_container, connection):
        result = asyncio.run(
            BenchmarkRunner.run_benchmark_with_evidence(
                MagicMock(),
                [
                    f'{shlex.quote(str(psql))} -X -v ON_ERROR_STOP=1 -f {shlex.quote(str(init))}',
                    f'{shlex.quote(str(pgbench))} -n -c 1 -t 500 -f {shlex.quote(str(script))}',
                ],
                db_conf=connection,
                command_timeout=60,
                connection_type=connection_type,
                connection=object() if connection_type == 'docker' else None,
                system_metrics_duration=2,
                system_metrics_interval=0.1,
            )
        )
        before = result['storage']['before_workload']
        after = result['storage']['after_workload']
        assert result['metrics']['transactions'] == 500
        assert result['metrics']['failed_transactions_percent'] == 0
        for name, column in [
            ('table_sizes', 'total_size_bytes'),
            ('index_sizes', 'index_size_bytes'),
        ]:
            assert rows(after, name)[0][column] > rows(before, name)[0][column]
        start = datetime.fromisoformat(result['workload']['started_at'])
        assert datetime.fromisoformat(before['completed_at']) <= start
        # ProcessResult uses wall time for its timestamp and a monotonic elapsed duration.
        assert datetime.fromisoformat(after['started_at']) >= start
        assert datetime.fromisoformat(after['started_at']) >= start + timedelta(
            seconds=result['workload']['elapsed_seconds'] - 0.01
        )
        if connection_type == 'docker':
            samples = [
                sample
                for provider_samples in result['system_metrics']['samples'].values()
                for sample in provider_samples
            ]
            assert samples
            assert max(datetime.fromisoformat(sample['timestamp']) for sample in samples) <= (
                datetime.fromisoformat(after['started_at'])
            )
        result['iteration'] = {'index': 1, 'parameter': 'clients', 'value': 1}
        report = {
            'report_name': 'actual-pgbench-storage',
            'benchmark_runs': [result],
            'sections': {'storage': build_storage_section([result])},
        }
        save_report(MagicMock(), report, tmp_path)
