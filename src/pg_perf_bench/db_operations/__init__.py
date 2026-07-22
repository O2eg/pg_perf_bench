from pathlib import PurePosixPath

from pg_perf_bench.const import ConnectionType
from pg_perf_bench.report.commands import collect_logs

from .conn_tasks import (
    DockerTasks,
    LocalConnTasks,
    SSHTasks,
    run_command,
    run_command_result,
)
from .db import DBTasks

__all__ = [
    'DBTasks',
    'SSHTasks',
    'DockerTasks',
    'LocalConnTasks',
    'run_command',
    'run_command_result',
]


async def collect_db_logs(logger, client, db_conn, report, local_logs_path=None):
    try:
        logger.info('Collection of database logs.')
        log_dir = await db_conn.fetchval('show log_directory')
        if not PurePosixPath(log_dir).is_absolute():
            data_dir = await db_conn.fetchval('show data_directory')
            log_dir = str(PurePosixPath(data_dir) / log_dir)
        kwargs = {'local_logs_path': local_logs_path} if local_logs_path is not None else {}
        log_report = await collect_logs(
            logger,
            client,
            log_dir,
            report['report_name'],
            **kwargs,
        )
        if log_report:
            result_section = report.setdefault('sections', {}).setdefault(
                'result',
                {
                    'header': 'Collected artifacts',
                    'description': 'Files collected during the run',
                    'state': 'expanded',
                    'reports': {},
                },
            )
            result_section.setdefault('reports', {}).update(log_report)
            logger.info('DB logs collected successfully.')

    except Exception as e:
        logger.error(f'Failed to collect DB logs: {e}')
        result_section = report.setdefault('sections', {}).setdefault(
            'result',
            {
                'header': 'Collected artifacts',
                'description': 'Files collected during the run',
                'state': 'expanded',
                'reports': {},
            },
        )
        result_section.setdefault('reports', {})['logs'] = {
            'header': 'database logs',
            'description': 'PostgreSQL log archive collection result',
            'item_type': 'plain_text',
            'state': 'collapsed',
            'data': f'Failed to collect database logs: {e}',
            'collection_status': 'error',
            'reason': str(e),
        }


def get_conn_type_tasks(type):
    if type == ConnectionType.SSH:
        return SSHTasks
    if type == ConnectionType.DOCKER:
        return DockerTasks
    if type == ConnectionType.LOCAL:
        return LocalConnTasks
