"""Self-contained HTML renderer for pg_perf_bench reports."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from pg_perf_bench.const import REPORT_TEMPLATE_FOLDER

_SCRIPT_END_RE = re.compile(r'</script', re.IGNORECASE)
_STYLE_END_RE = re.compile(r'</style', re.IGNORECASE)


def render_html(report: dict[str, Any]) -> str:
    """Render a report as one portable HTML document with embedded assets."""
    if not isinstance(report, dict):
        raise ValueError('Report object must be a dictionary.')

    title_value = report.get('header') or report.get('report_name') or 'pg_perf_bench report'
    replacements = {
        '__TITLE__': html.escape(str(title_value)),
        '__PAYLOAD__': _safe_json_payload(report),
        '__ECHARTS_JS__': _inline_script(_read_template_resource('vendor', 'echarts-6.1.0.min.js')),
        '__HIGHLIGHT_JS__': _inline_script(
            _read_template_resource('vendor', 'highlight-11.11.1.min.js')
        ),
        '__HIGHLIGHT_CSS__': _inline_style(
            _read_template_resource('vendor', 'highlight-github-dark-11.11.1.min.css')
        ),
        '__THIRD_PARTY_LICENSES__': _inline_script(_third_party_licenses()),
    }
    placeholder_pattern = re.compile('|'.join(re.escape(key) for key in replacements))
    return placeholder_pattern.sub(lambda match: replacements[match.group(0)], _html_template())


def render_from_json(json_path: str | Path, html_path: str | Path) -> None:
    """Render a JSON report from disk to a self-contained HTML file."""
    report = json.loads(Path(json_path).read_text(encoding='utf-8'))
    destination = Path(html_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{destination.name}.',
        suffix='.tmp',
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(render_html(report))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_json_payload(report: dict[str, Any]) -> str:
    payload = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
    )
    return (
        payload.replace('&', '\\u0026')
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
        .replace('\u2028', '\\u2028')
        .replace('\u2029', '\\u2029')
    )


@lru_cache(maxsize=1)
def _html_template() -> str:
    return _read_template_resource('report.html')


@cache
def _read_template_resource(*path_parts: str) -> str:
    path = REPORT_TEMPLATE_FOLDER.joinpath(*path_parts)
    return path.read_text(encoding='utf-8')


def _inline_script(value: str) -> str:
    return _SCRIPT_END_RE.sub('<\\/script', value)


def _inline_style(value: str) -> str:
    return _STYLE_END_RE.sub('<\\/style', value)


@lru_cache(maxsize=1)
def _third_party_licenses() -> str:
    sections = [
        _read_template_resource('vendor', 'THIRD_PARTY_LICENSES.txt'),
        'Apache ECharts 6.1.0 - Apache-2.0 license\n\n'
        + _read_template_resource('vendor', 'echarts-6.1.0.LICENSE.txt'),
        'Apache ECharts 6.1.0 - NOTICE\n\n'
        + _read_template_resource('vendor', 'echarts-6.1.0.NOTICE.txt'),
        'Apache ECharts 6.1.0 embedded d3 components - BSD-3-Clause license\n\n'
        + _read_template_resource('vendor', 'echarts-6.1.0.LICENSE-d3.txt'),
        'highlight.js 11.11.1 - BSD-3-Clause license\n\n'
        + _read_template_resource('vendor', 'highlight-11.11.1.LICENSE.txt'),
    ]
    return '\n\n'.join(section.rstrip() for section in sections) + '\n'
