import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from pg_perf_bench.collect_info import InfoCollector
from pg_perf_bench.const import DB_INFO_TEMPLATE_JSON_PATH
from pg_perf_bench.db_operations import collect_db_logs


def test_database_collection_only_opens_read_only_connection():
    client = MagicMock()
    client.send_pg_config_file = AsyncMock()
    connection = MagicMock()
    connect = AsyncMock(return_value=connection)
    db_conf = {
        'db_env': {
            'pg_data_path': '/var/lib/postgresql/data',
            'pg_custom_config': '/tmp/postgresql.conf',
        },
        'db_conn_params': {
            'host': '127.0.0.1',
            'port': 5432,
            'user': 'postgres',
            'password': 'secret',
            'database': 'postgres',
            'timeout': 3,
        },
    }

    async def scenario():
        with patch(
            'pg_perf_bench.collect_info.asyncpg.connect',
            connect,
        ):
            result = await InfoCollector.handle_db_info(
                client,
                'local',
                db_conf,
                MagicMock(),
            )
        assert result is connection

    asyncio.run(scenario())
    client.send_pg_config_file.assert_not_awaited()
    kwargs = connect.await_args.kwargs
    assert kwargs['server_settings']['default_transaction_read_only'] == 'on'
    assert kwargs['server_settings']['statement_timeout'] == '10000'


def test_collect_info_closes_database_connection_when_collection_fails():
    database = MagicMock()
    database.close = AsyncMock()
    transport = MagicMock()
    transport.__aenter__ = AsyncMock(return_value=transport)
    transport.__aexit__ = AsyncMock(return_value=None)
    transport_class = MagicMock(return_value=transport)

    async def scenario():
        with (
            patch(
                'pg_perf_bench.collect_info.get_connection',
                return_value=transport_class,
            ),
            patch.object(
                InfoCollector,
                'handle_db_info',
                new=AsyncMock(return_value=database),
            ),
            patch.object(
                InfoCollector,
                'collect_monitoring_info',
                new=AsyncMock(side_effect=RuntimeError('collection failed')),
            ),
        ):
            try:
                await InfoCollector.collect_info(
                    args={'mode': 'collect-db-info'},
                    conn_type='local',
                    conn_conf={},
                    db_conf={},
                    report_conf={
                        'report_name': 'test',
                        'report_template': DB_INFO_TEMPLATE_JSON_PATH,
                    },
                    log_conf={},
                    logger=MagicMock(),
                )
            except RuntimeError as exc:
                assert str(exc) == 'collection failed'
            else:
                raise AssertionError('collection failure was swallowed')

    asyncio.run(scenario())
    database.close.assert_awaited_once()


def test_requested_log_collection_failure_is_visible_in_report():
    database = MagicMock()
    database.fetchval = AsyncMock(side_effect=RuntimeError('log directory unavailable'))
    report = {'report_name': 'test', 'sections': {}}

    asyncio.run(collect_db_logs(MagicMock(), MagicMock(), database, report))

    item = report['sections']['result']['reports']['logs']
    assert item['collection_status'] == 'error'
    assert item['reason'] == 'log directory unavailable'
