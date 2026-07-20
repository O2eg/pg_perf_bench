import asyncio
import json
import os
import re

from pg_perf_bench.const import (
    DEFAULT_LOG_ARCHIVE_NAME,
    LOCAL_DB_LOGS_PATH,
    SHELL_COMMANDS_PATH,
    SQL_COMMANDS_PATH,
    WorkloadTypes,
)
from pg_perf_bench.contracts import redact_mapping
from pg_perf_bench.report.processing import parse_json_in_order


def _command_source(base_path, file_name: str):
    if os.path.basename(file_name) != file_name or file_name in {'', '.', '..'}:
        raise ValueError(f'Unsafe command file name: {file_name!r}')
    return base_path / file_name


def get_script_text(full_script_path) -> str:
    # check if file exists before reading
    if not os.path.exists(full_script_path):
        raise FileNotFoundError(f'Script file not found: {full_script_path}')

    try:
        with open(full_script_path, encoding='utf-8') as file_content:
            return file_content.read()
    except OSError as e:
        raise OSError(f'Failed to open or read script file {full_script_path}: {e}') from e


async def _run_transport_command(conn, script: str, item: dict) -> str:
    timeout = item.get('timeout_seconds')
    if timeout is None:
        return await conn.run_command(script, True)
    return await conn.run_command(script, True, timeout=timeout)


async def run_shell_command(logger, conn, item):
    # check necessary fields in item
    shell_cmd_file = item.get('shell_command_file')
    if not shell_cmd_file:
        logger.debug('Missing "shell_command_file" in item. Skipping execution.')
        return

    try:
        raw_script_text = get_script_text(_command_source(SHELL_COMMANDS_PATH, shell_cmd_file))
    except Exception as e:
        item['collection_status'] = 'error'
        item['reason'] = str(e)
        item['data'] = f'Error loading shell command: {e}'
        item['item_type'] = 'plain_text'
        return

    if item.get('item_type') == 'plain_text':
        try:
            result = await _run_transport_command(conn, raw_script_text, item)
            if result is None:
                item['data'] = 'Error generating report of type "plain_text": No output returned.'
                item['collection_status'] = 'empty'
            else:
                item['data'] = result
                item['collection_status'] = 'ok' if result else 'empty'
        except Exception as e:
            logger.debug(f'Error generating report of type "plain_text": {e}')
            item['data'] = f'Error generating report of type "plain_text": {e}'
            item['collection_status'] = 'error'
            item['reason'] = str(e)
    elif item.get('item_type') == 'table':
        try:
            data_str = await _run_transport_command(conn, raw_script_text, item)
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError as e:
                logger.debug(f'Error parsing JSON for table report:\n{e}')
                item['data'] = f'Error parsing JSON for table report: {e}'
                item['item_type'] = 'plain_text'
                item['collection_status'] = 'error'
                item['reason'] = str(e)
                return

            if not isinstance(data, list):
                logger.debug(
                    'Unexpected data format from shell command. A list of dicts is expected.'
                )
                item['data'] = (
                    'Unexpected data format from shell command. A list of dicts is expected.'
                )
                item['item_type'] = 'plain_text'
                item['collection_status'] = 'error'
                item['reason'] = 'Expected a list of objects'
                return

            # Ensure 'theader' is a list in item
            if 'theader' not in item or not isinstance(item['theader'], list):
                item['theader'] = []

            if 'data' not in item or not isinstance(item['data'], list):
                item['data'] = []

            valid_objects = [obj for obj in data if isinstance(obj, dict)]
            invalid_count = len(data) - len(valid_objects)
            for obj in valid_objects:
                for key in obj.keys():
                    if key not in item['theader']:
                        item['theader'].append(key)

            for obj in valid_objects:
                item['data'].append([obj.get(key, None) for key in item['theader']])
            if invalid_count:
                item['collection_status'] = 'partial'
                item['reason'] = f'Skipped {invalid_count} non-object row(s) from shell output'
            else:
                item['collection_status'] = 'ok' if item['data'] else 'empty'
        except Exception as e:
            logger.debug(f'Error generating report of type "table": {e}')
            item['data'] = f'Error generating report of type "table": {e}'
            item['item_type'] = 'plain_text'
            item['collection_status'] = 'error'
            item['reason'] = str(e)


async def run_sql_command(logger, dbconn, item):
    sql_cmd_file = item.get('sql_command_file')
    if not sql_cmd_file:
        logger.debug('Missing "sql_command_file" in item. Skipping execution.')
        return

    try:
        raw_script_text = get_script_text(_command_source(SQL_COMMANDS_PATH, sql_cmd_file))
    except Exception as e:
        item['collection_status'] = 'error'
        item['reason'] = str(e)
        item['data'] = f'Error loading SQL command: {e}'
        item['item_type'] = 'plain_text'
        return
    item_type = item.get('item_type')
    query_timeout = float(item.get('timeout_seconds', 10.0))
    if item_type == 'plain_text':
        try:
            result = await asyncio.wait_for(
                dbconn.fetchval(raw_script_text),
                timeout=query_timeout,
            )
            if result is None:
                item['data'] = 'Error generating report of type "plain_text": No data returned.'
            else:
                item['data'] = result
                item['collection_status'] = 'ok'
        except Exception as e:
            logger.debug(f'Error generating report of type "plain_text": {e}')
            item['data'] = f'Error generating report of type "plain_text": {e}'
            item['collection_status'] = 'error'
            item['reason'] = str(e)
    elif item_type == 'table':
        try:
            fetch_result = await asyncio.wait_for(
                dbconn.fetch(raw_script_text),
                timeout=query_timeout,
            )
            if fetch_result:
                item['theader'] = [key for key in fetch_result[0].keys()]
                item['data'] = [list(record) for record in fetch_result]
                item['collection_status'] = 'ok'
            else:
                item['theader'] = []
                item['data'] = 'No rows returned by the SQL query.'
                item['item_type'] = 'plain_text'
                item['collection_status'] = 'empty'
        except Exception as e:
            logger.debug(f'Error generating report of type "table": {e}')
            item['theader'] = []
            item['data'] = f'Error generating report of type "table": {e}'
            item['item_type'] = 'plain_text'
            item['collection_status'] = 'error'
            item['reason'] = str(e)
    else:
        logger.debug(f'Unknown item_type in sql_command: {item_type}')


def args(report_data, item):
    # build a table of (arg, value) from report_data['args']
    if 'args' not in report_data or not isinstance(report_data['args'], dict):
        item['data'] = "No 'args' in report_data or invalid type"
        item['collection_status'] = 'error'
        return

    safe_args = redact_mapping(report_data['args'])
    item['theader'] = ['arg', 'value']
    item['data'] = [[key, str(value)] for key, value in safe_args.items()]
    item['collection_status'] = 'ok'


def get_workload_cmds(report_data):
    benchmark_runs = report_data.get('benchmark_runs')
    if isinstance(benchmark_runs, list):
        commands = [
            str(run.get('workload', {}).get('command'))
            for run in benchmark_runs
            if isinstance(run, dict) and run.get('workload', {}).get('command')
        ]
        if commands:
            return commands
    # retrieve workload commands with replaced placeholders
    workload_conf = report_data.get('workload_conf', {})
    if not workload_conf or not isinstance(workload_conf, dict):
        return []

    workload_command = workload_conf.get('workload_command')
    iter_list = workload_conf.get('pgbench_iter_list')
    iter_name = workload_conf.get('pgbench_iter_name')

    if not workload_command or not iter_list or not iter_name:
        return []

    pgbench_cmds = []
    placeholder = f'ARG_{str(iter_name).upper()}'
    for iteration in iter_list:
        workload_cmd_iter = workload_command.replace(placeholder, str(iteration))
        pgbench_cmds.append(workload_cmd_iter)

    return pgbench_cmds


def pgbench_options_table(report_data, item):
    # build a table "iteration number -> pgbench_options"
    pgbench_cmds = get_workload_cmds(report_data)
    item['theader'] = ['iteration number', 'pgbench_options']
    item['data'] = [[idx, cmd] for idx, cmd in enumerate(pgbench_cmds)]
    item['collection_status'] = 'ok' if item['data'] else 'empty'


def workload_parse(report_data, item, phase='workload'):
    """
    Process workload data for either 'init' or 'workload' phase.

    Args:
        report_data: The report data dictionary
        item: The item to store results in
        phase: Either 'init' or 'workload' to determine which phase to process
    """
    # Get command key based on phase
    command_key = 'init_command' if phase == 'init' else 'workload_command'

    # Get workload configuration
    workload_conf = report_data.get('workload_conf', {})
    if not workload_conf or not isinstance(workload_conf, dict):
        item['data'] = "No 'workload_conf' found in report_data"
        item['collection_status'] = 'error'
        return

    btype = workload_conf.get('benchmark_type')
    if btype == WorkloadTypes.CUSTOM:
        command = str(workload_conf.get(command_key, ''))
        command = command.replace('ARG_WORKLOAD_PATH', str(workload_conf.get('workload_path', '')))
        pattern = re.compile(r'(?:(?:-f|--file=)\s*)?(\S+\.sql)')
        matches = pattern.findall(command)
        matches = [match for match in matches if match]

        data = ''
        for m in matches:
            if not os.path.exists(m):
                data += f'File not found: {m}\n\n'
                continue
            try:
                with open(m, encoding='utf-8') as f:
                    content = f.read()
                data += f'{m} :\n{content}\n\n'
            except OSError as e:
                data += f'Error reading file {m}: {str(e)}\n\n'

        item['data'] = data

    elif btype == WorkloadTypes.DEFAULT:
        item['data'] = str(workload_conf.get(command_key, ''))

    else:
        item['data'] = f'Unknown or missing benchmark_type: {btype}'

    item['collection_status'] = 'ok' if item.get('data') else 'empty'


# Wrapper functions to maintain backward compatibility
def workload_tables(report_data, item):
    """Process initial command parse data"""
    workload_parse(report_data, item, phase='init')


def workload(report_data, item):
    """Process workload command parse data"""
    workload_parse(report_data, item, phase='workload')


def benchmark_result(report_data, item):
    # turn 'pgbench_outputs' into a table
    if 'pgbench_outputs' not in report_data:
        item['data'] = "No 'pgbench_outputs' in report_data"
        item['collection_status'] = 'error'
        return

    results = report_data['pgbench_outputs']
    if not isinstance(results, list):
        item['data'] = 'pgbench_outputs is not a list'
        item['collection_status'] = 'error'
        return

    item['theader'] = [
        'clients',
        'duration',
        'number of transactions actually processed',
        'latency average',
        'initial connection time',
        'tps',
    ]
    item['data'] = results
    invalid_results = sum(
        1
        for result in results
        if not isinstance(result, list)
        or len(result) < 6
        or not isinstance(result[5], (int, float))
    )
    if invalid_results:
        item['collection_status'] = 'partial'
        item['reason'] = f'TPS could not be parsed for {invalid_results} benchmark iteration(s)'
    else:
        item['collection_status'] = 'ok' if results else 'empty'


def chart_tps(report_data, item):
    # fill chart data with tps vs iteration
    if (
        'workload_conf' not in report_data
        or 'pgbench_iter_list' not in report_data['workload_conf']
    ):
        item['data'] = "Missing 'workload_conf' or 'pgbench_iter_list'"
        item['collection_status'] = 'error'
        return

    if 'pgbench_outputs' not in report_data:
        item['data'] = "Missing 'pgbench_outputs'"
        item['collection_status'] = 'error'
        return

    iter_list = report_data['workload_conf']['pgbench_iter_list']
    outputs = report_data['pgbench_outputs']

    if not isinstance(iter_list, list) or not isinstance(outputs, list):
        item['data'] = "Invalid 'pgbench_iter_list' or 'pgbench_outputs' type"
        item['collection_status'] = 'error'
        return

    if len(iter_list) != len(outputs):
        item['data'] = "Mismatched length between 'pgbench_iter_list' and 'pgbench_outputs'"
        item['collection_status'] = 'error'
        return

    param_name = report_data['workload_conf'].get('pgbench_iter_name', 'iteration')
    report_name = report_data.get('report_conf', {}).get('report_name', 'N/A')

    # build the chart structure in 'item["data"]'
    item['data'].update(
        {
            'title': {'text': f'tps({param_name})'},
            'xaxis': {'title': {'text': param_name}},
            'series': [
                {
                    'name': f'{report_name},tps',
                    'data': [
                        [x, round(val[5], 1)]
                        for x, val in zip(iter_list, outputs, strict=True)
                        if (
                            isinstance(val, list)
                            and len(val) >= 6
                            and isinstance(val[5], (int, float))
                        )
                    ],
                }
            ],
        }
    )
    points = item['data']['series'][0]['data']
    missing_points = len(outputs) - len(points)
    if missing_points:
        item['collection_status'] = 'partial'
        item['reason'] = f'TPS could not be plotted for {missing_points} benchmark iteration(s)'
    else:
        item['collection_status'] = 'ok' if points else 'empty'


async def collect_logs(
    logger,
    connect,
    remote_logs_path,
    report_name: str = DEFAULT_LOG_ARCHIVE_NAME,
):
    # check if remote_logs_path is provided
    if not remote_logs_path:
        if logger:
            logger.warning('Database log path is not provided; skipping.')
        return None

    if logger:
        logger.info(f'Copying logs from {remote_logs_path} -> {LOCAL_DB_LOGS_PATH}/{report_name}')

    data = await connect.copy_db_log_files(remote_logs_path, LOCAL_DB_LOGS_PATH, report_name)
    if data:
        report_item = {
            'header': 'database logs',
            'description': 'Local path to the database log archive',
            'item_type': 'link',
            'state': 'collapsed',
            'python_command': '',
            'data': data,
        }

        if logger:
            logger.info(f'The log archive has been collected to: {report_item["data"]}')
        return {'logs': report_item}
    else:
        if logger:
            logger.error('Error collecting database log files')
        return None


PYTHON_REPORT_COMMANDS = {
    'args': args,
    'pgbench_options_table': pgbench_options_table,
    'workload_tables': workload_tables,
    'workload': workload,
    'benchmark_result': benchmark_result,
    'chart_tps': chart_tps,
}


async def execute_steps_in_order(logger, command_steps, report_data, conn, db) -> None:
    # validate command_steps is a list
    if not isinstance(command_steps, list):
        if logger:
            logger.error('Command_steps is not a valid list. Skipping.')
        return

    for step in command_steps:
        cmd_type = step.get('cmd_type')
        report_obj = step.get('report_obj')
        section_name = step.get('section')
        report_name = step.get('report')
        cmd_value = step.get('cmd_value')

        if logger:
            logger.debug(
                f'Executing step - Section: {section_name}, Report: {report_name}, '
                f'Command Type: {cmd_type}, Command: {cmd_value}'
            )
        if not report_obj:
            if logger:
                logger.warning("Missing 'report_obj' in step. Skipping.")
            continue

        try:
            if cmd_type == 'shell_command':
                await run_shell_command(logger, conn, report_obj)

            elif cmd_type == 'sql_command':
                if db:
                    await run_sql_command(logger, db, report_obj)
                else:
                    report_obj['collection_status'] = 'skipped'
                    report_obj['reason'] = 'No database connection provided'
                    if logger:
                        logger.error('No database connection provided for sql_command. Skipping.')

            elif cmd_type == 'python_command':
                func_name = report_obj.get('python_command')
                possible_func = PYTHON_REPORT_COMMANDS.get(func_name)
                if callable(possible_func):
                    possible_func(report_data, report_obj)
                    report_obj.setdefault('collection_status', 'ok')
                else:
                    report_obj['data'] = f'Not found or is not a function: {func_name}'
                    report_obj['collection_status'] = 'error'
            else:
                report_obj['data'] = f'Unknown command type {cmd_type}'
                report_obj['collection_status'] = 'error'
        except Exception as exc:
            logger.error(
                'Report item %s.%s failed: %s',
                section_name,
                report_name,
                exc,
            )
            report_obj['data'] = f'Execution error: {exc}'
            report_obj['collection_status'] = 'error'
            report_obj['reason'] = str(exc)


async def fill_info_report(logger, conn, db, workload_conf, report):
    # We parse the report to get steps in order
    command_steps, _ = parse_json_in_order(report)
    if not command_steps:
        # If there's nothing to process
        return
    # Then execute them as usual
    await execute_steps_in_order(logger, command_steps, workload_conf, conn, db)
