import platform
import sys

import asyncpg

from pg_perf_bench import __version__
from pg_perf_bench.connections.common import get_connection
from pg_perf_bench.const import (
    WorkMode,
    get_datetime_report,
    get_default_report_name,
)
from pg_perf_bench.contracts import ARTIFACT_SCHEMA_VERSION
from pg_perf_bench.db_operations import collect_db_logs
from pg_perf_bench.errors import CollectionError
from pg_perf_bench.log import display_user_configuration
from pg_perf_bench.report.commands import fill_info_report
from pg_perf_bench.report.processing import get_report_structure


class InfoCollector:
    """
    A static utility class to collect system and database configuration info,
    generate reports, and optionally collect PostgreSQL logs.
    """

    @staticmethod
    async def prepare_report(report_conf: dict, logger) -> dict:
        """
        Loads the report template and sets its metadata fields.
        """
        template_path = report_conf['report_template']
        report = get_report_structure(template_path)
        report['description'] = get_datetime_report('%d/%m/%Y %H:%M:%S')

        report['report_name'] = (
            report_conf.get('report_name')
            or f'{report_conf.get("mode", "collect")}-{get_default_report_name()}'
        )
        report['artifact_schema_version'] = ARTIFACT_SCHEMA_VERSION
        report['generator'] = {'name': 'pg_perf_bench', 'version': __version__}
        report['runtime'] = {
            'python': sys.version.split()[0],
            'platform': platform.platform(),
        }
        return report

    @staticmethod
    async def handle_db_info(_client, _conn_type: str, db_conf: dict, logger):
        """
        Opens a read-only database connection for collecting metrics.

        Collection modes deliberately don't install configuration files or control
        the PostgreSQL service. Those actions belong to the destructive benchmark
        workflow only.
        Returns an asyncpg connection or None.
        """
        db_conn = None
        db_conn_params = db_conf.get('db_conn_params')
        if db_conn_params and isinstance(db_conn_params, dict):
            try:
                connection_params = dict(db_conn_params)
                connection_params['server_settings'] = {
                    'default_transaction_read_only': 'on',
                    'statement_timeout': '10000',
                }
                db_conn = await asyncpg.connect(**connection_params)
            except Exception as e:
                raise CollectionError(f'Failed to connect to DB: {e}') from e
        else:
            logger.warning("No valid 'db_conn_params' found. Skipping DB actions.")
        return db_conn

    @staticmethod
    async def collect_monitoring_info(logger, client, db_conn, report_data: dict, report: dict):
        """
        Fills the report with system and database monitoring info.
        """
        await fill_info_report(logger, client, db_conn, report_data, report)
        logger.info('Monitoring info collected successfully.')

    @staticmethod
    async def collect_logs_if_needed(
        args: dict, log_conf: dict, logger, client, db_conn, report: dict
    ):
        """
        Collects PostgreSQL logs if requested in configuration.
        """
        if args['mode'] in [
            WorkMode.COLLECT_DB_INFO,
            WorkMode.COLLECT_ALL_INFO,
        ]:
            if log_conf.get('collect_pg_logs') and db_conn:
                await collect_db_logs(logger, client, db_conn, report)

    @staticmethod
    async def collect_info(
        args: dict,
        conn_type: str,
        conn_conf: dict,
        db_conf: dict,
        report_conf: dict,
        log_conf: dict,
        logger,
    ) -> dict | None:
        """
        Orchestrates full process of collecting system and DB info,
        generating and returning a report dictionary.
        """
        if 'report_template' not in report_conf:
            logger.error('Missing "report_template" in report_conf.')
            return None

        display_user_configuration(args, logger)

        try:
            # Step 1: Prepare report structure
            report_conf = {**report_conf, 'mode': args['mode']}
            report = await InfoCollector.prepare_report(report_conf, logger)
            logger.info('Report template loaded successfully.')
            report_data = {'args': args, 'report_conf': report_conf}

            # Step 2: Initialize connection object
            connection_class = get_connection(conn_type)
            if not connection_class:
                logger.error(f'Unknown connection type: {conn_type}. Cannot proceed.')
                return None
            logger.info(f'Connection type selected: {conn_type}')

            connection = connection_class(**conn_conf)
            connection.logger = logger

            db_conn = None
            async with connection as client:
                logger.info('Connection established successfully.')
                # Step 3: Handle optional DB setup and connection
                if args['mode'] in [
                    WorkMode.COLLECT_DB_INFO,
                    WorkMode.COLLECT_ALL_INFO,
                ]:
                    db_conn = await InfoCollector.handle_db_info(client, conn_type, db_conf, logger)
                    if db_conn is None:
                        raise CollectionError('Database connection was not established')
                    logger.info('Database connection established for DB collection.')

                # Step 4: Collect metrics
                await InfoCollector.collect_monitoring_info(
                    logger, client, db_conn, report_data, report
                )

                # Step 5: Optionally collect PostgreSQL logs
                await InfoCollector.collect_logs_if_needed(
                    args, log_conf, logger, client, db_conn, report
                )

                if db_conn is not None:
                    await db_conn.close()
                    db_conn = None
                    logger.info('Database connection closed.')

            logger.info('Collect info process completed successfully.')
            return report

        except FileNotFoundError as fe:
            logger.error(f'File not found error: {str(fe)}')
            logger.error('No report has been generated due to missing template.')
            raise CollectionError(str(fe)) from fe
        except Exception as e:
            logger.error(f'Unexpected error in collect_info: {str(e)}')
            logger.error('Emergency termination. No report has been generated.')
            raise
        finally:
            if 'db_conn' in locals() and db_conn is not None:
                await db_conn.close()
