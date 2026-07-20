import glob
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pg_perf_bench.const import LOGS_FOLDER, LogLevel
from pg_perf_bench.contracts import redact_mapping


def display_user_configuration(raw_args, logger):
    safe_args = redact_mapping(raw_args)
    message_lines: list[str] = ['Incoming parameters:']
    message_lines.extend(
        f'#   {name} = {value}' for name, value in safe_args.items() if value is not None
    )
    message_lines.append(f'#{"-" * 35}')
    logger.info('\n'.join(message_lines))


def setup_logger(
    raw_log_level,
    arg_clear_logs=False,
    *,
    stream=None,
    log_dir: str | os.PathLike[str] | None = None,
):
    destination = Path(log_dir) if log_dir is not None else LOGS_FOLDER
    if arg_clear_logs:
        clear_logs(destination)
    file_name = f'{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.log'
    destination.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        '{asctime} {levelname:>10s} {name:>35s} : {lineno:-4d} - {message}',
        style='{',
    )
    log_level = LogLevel(raw_log_level)
    log = logging.getLogger('pg_perf_bench')
    for handler in log.handlers:
        handler.close()
    log.handlers.clear()
    stream_handler = logging.StreamHandler(stream or sys.stdout)
    file_handler = RotatingFileHandler(
        destination / file_name,
        maxBytes=1024 * 10000,
        backupCount=10,
        encoding='utf-8',
    )
    stream_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    log.addHandler(stream_handler)
    log.addHandler(file_handler)
    log.propagate = False
    logging.getLogger('asyncssh').setLevel(logging.CRITICAL)
    logging.getLogger('docker').setLevel(logging.CRITICAL)
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)
    logging.getLogger('asyncio').setLevel(logging.CRITICAL)

    if (log_level_int := log_level.as_level_int_value()) is None:
        log.setLevel(logging.INFO)
        log.error('Incorrectly specified --log-level, automatically set to "info" level.')
    log.setLevel(log_level_int)
    log.info('Logging level: %s', log_level)
    if arg_clear_logs:
        log.info('Clearing logs folder.')
    return log


def clear_logs(log_dir: str | os.PathLike[str] = LOGS_FOLDER):
    files = glob.glob(str(Path(log_dir) / '*.log'))
    for f in files:
        os.remove(f)
