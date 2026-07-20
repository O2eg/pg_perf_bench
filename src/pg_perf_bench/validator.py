"""Static validation for packaged report content."""

from __future__ import annotations

import json
from pathlib import Path

from pg_perf_bench.const import (
    JOIN_TASKS_PATH,
    REPORT_TEMPLATE_FOLDER,
    SHELL_COMMANDS_PATH,
    SQL_COMMANDS_PATH,
)
from pg_perf_bench.report.processing import get_report_structure, parse_json_in_order

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
    join_tasks = sorted(JOIN_TASKS_PATH.glob('*.json'))
    if not join_tasks:
        errors.append(f'No join task definitions found in {JOIN_TASKS_PATH}')
    for task_path in join_tasks:
        try:
            task = json.loads(task_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f'{task_path}: {exc}')
            continue
        items = task.get('items') if isinstance(task, dict) else None
        if (
            not isinstance(items, list)
            or not items
            or not all(isinstance(item, str) and item.strip() for item in items)
        ):
            errors.append(f'{task_path}: items must be a non-empty list of dotted paths')
    return errors
