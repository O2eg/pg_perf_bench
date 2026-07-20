"""Report collection, persistence, and self-contained HTML rendering."""

from pg_perf_bench.report.html import render_from_json, render_html
from pg_perf_bench.report.processing import get_report_structure, save_report

__all__ = [
    'get_report_structure',
    'render_from_json',
    'render_html',
    'save_report',
]
