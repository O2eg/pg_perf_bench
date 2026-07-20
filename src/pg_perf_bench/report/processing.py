"""Report template loading and atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pg_perf_bench.const import REPORT_FOLDER
from pg_perf_bench.contracts import redact_mapping
from pg_perf_bench.errors import ReportError
from pg_perf_bench.report.html import render_html


def get_report_structure(file_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f'Report template file not found at: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f'Cannot load report template {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise ValueError('Invalid report structure: root must be a JSON object.')
    return data


def validate_report_name(value: object) -> str:
    name = str(value or '').strip()
    if not name:
        raise ValueError("The 'report_name' field is missing or empty.")
    if name in {'.', '..'} or Path(name).name != name or '\x00' in name:
        raise ValueError(f'Unsafe report name: {name!r}')
    return name


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.',
        suffix='.tmp',
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_json_report(new_report_path, report_json):
    try:
        payload = json.dumps(
            report_json,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        _atomic_write(Path(new_report_path), payload + '\n')
    except (OSError, TypeError, ValueError) as exc:
        raise ReportError(f'Failed to write JSON report to {new_report_path}: {exc}') from exc


def _save_html_report(new_report_json_path: str, path) -> None:
    source = Path(new_report_json_path)
    if not source.is_file():
        raise FileNotFoundError(f'JSON report file not found at: {source}')
    try:
        report = json.loads(source.read_text(encoding='utf-8'))
        _atomic_write(Path(path), render_html(report))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReportError(f'Failed to write HTML report: {exc}') from exc


def save_report(logger, report_struct, dest_dir='') -> dict[str, str]:
    report_name = validate_report_name(report_struct.get('report_name'))
    safe_report = redact_mapping(report_struct)
    destination = Path(dest_dir) if dest_dir else REPORT_FOLDER
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / f'{report_name}.json'
    html_path = destination / f'{report_name}.html'

    try:
        json_payload = (
            json.dumps(
                safe_report,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            + '\n'
        )
        html_payload = render_html(safe_report)
        _atomic_write(json_path, json_payload)
        _atomic_write(html_path, html_payload)
    except (OSError, TypeError, ValueError) as exc:
        raise ReportError(f'Failed to persist report {report_name!r}: {exc}') from exc

    logger.info('Reports generated: %s, %s', json_path, html_path)
    return {'json': str(json_path.resolve()), 'html': str(html_path.resolve())}


def dump_updated_json(data: dict, output_file: str) -> None:
    _save_json_report(output_file, data)


def parse_json_in_order(report) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(report, dict):
        raise ValueError('Report object must be a dictionary.')
    command_steps: list[dict[str, Any]] = []
    sections = report.get('sections')
    if not isinstance(sections, dict):
        return command_steps, report
    for section_name, section_obj in sections.items():
        if not isinstance(section_obj, dict):
            continue
        reports = section_obj.get('reports', {})
        if not isinstance(reports, dict):
            continue
        for report_name, report_obj in reports.items():
            if not isinstance(report_obj, dict):
                continue
            for field, command_type in (
                ('shell_command_file', 'shell_command'),
                ('sql_command_file', 'sql_command'),
                ('python_command', 'python_command'),
            ):
                if field in report_obj:
                    command_steps.append(
                        {
                            'section': section_name,
                            'report': report_name,
                            'cmd_type': command_type,
                            'cmd_value': report_obj[field],
                            'report_obj': report_obj,
                        }
                    )
                    break
    return command_steps, report
