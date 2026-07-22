"""Static validation for packaged report content."""

from __future__ import annotations

from pathlib import Path

from pg_perf_bench.const import REPORT_TEMPLATE_FOLDER, SHELL_COMMANDS_PATH, SQL_COMMANDS_PATH
from pg_perf_bench.join_catalog import validate_join_task_catalog
from pg_perf_bench.report.processing import get_report_structure, parse_json_in_order
from pg_perf_bench.workloads import validate_workload_profiles

PYTHON_COMMANDS = frozenset(
    {
        'args',
        'pgbench_options_table',
        'workload_tables',
        'workload',
        'benchmark_result',
        'chart_tps',
    }
)


def validate_content() -> list[str]:
    errors: list[str] = []
    templates = sorted(REPORT_TEMPLATE_FOLDER.glob('*_report_struct.json'))
    if not templates:
        return [f'No report templates found in {REPORT_TEMPLATE_FOLDER}']
    for template in templates:
        try:
            report = get_report_structure(template)
            steps, _ = parse_json_in_order(report)
        except Exception as exc:
            errors.append(f'{template}: {exc}')
            continue
        for step in steps:
            command_type = step['cmd_type']
            command_value = str(step['cmd_value'])
            location = f'{template.name}:{step["section"]}.{step["report"]}'
            if Path(command_value).name != command_value:
                errors.append(f'{location}: command path must be a file name')
                continue
            if command_type == 'shell_command':
                source = SHELL_COMMANDS_PATH / command_value
                if not source.is_file():
                    errors.append(f'{location}: missing shell command {command_value}')
            elif command_type == 'sql_command':
                source = SQL_COMMANDS_PATH / command_value
                if not source.is_file():
                    errors.append(f'{location}: missing SQL command {command_value}')
            elif command_value not in PYTHON_COMMANDS:
                errors.append(f'{location}: unknown Python command {command_value}')
    errors.extend(validate_join_task_catalog())
    errors.extend(validate_workload_profiles())
    return errors
