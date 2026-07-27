import json
import re
from pathlib import Path
from unittest.mock import MagicMock

from pg_perf_bench.report.html import render_html
from pg_perf_bench.report.processing import save_report


def sample_report() -> dict:
    return {
        'header': 'PostgreSQL benchmark report',
        'report_name': 'smoke_report',
        'description': 'Current report content',
        'generator': {'name': 'pg_perf_bench', 'version': '0.2.1'},
        'invocation': {
            'mode': 'benchmark',
            'connection_type': 'local_docker',
            'database': {
                'host': '127.0.0.1',
                'port': 5432,
                'name': 'postgres',
                'user': 'postgres',
            },
            'workload': {
                'profile': 'pagila',
                'scale': 0.1,
                'duration_seconds': 30,
                'iteration_parameter': 'pgbench_clients',
                'iteration_values': [1, 2],
            },
            'metrics': {'engine': 'pg_diag', 'interval_seconds': 1.0},
            'safety': {
                'database_recreated_before_each_iteration': True,
                'os_caches_dropped_before_each_iteration': False,
            },
        },
        'sections': {
            'result': {
                'header': 'Test results',
                'description': 'Measurements',
                'state': 'expanded',
                'reports': {
                    'output': {
                        'header': 'pgbench output',
                        'description': '',
                        'state': 'collapsed',
                        'item_type': 'plain_text',
                        'data': 'transactions: 42',
                    },
                    'measurements': {
                        'header': 'Benchmark results',
                        'description': '',
                        'state': 'expanded',
                        'item_type': 'table',
                        'theader': ['clients', 'tps'],
                        'data': [[1, 120.5], [2, 210.25]],
                    },
                    'chart': {
                        'header': 'Benchmark plots',
                        'description': '',
                        'state': 'expanded',
                        'item_type': 'chart',
                        'data': {
                            'series': [
                                {
                                    'name': 'smoke_report,tps',
                                    'data': [[1, 120.5], [2, 210.25]],
                                }
                            ],
                            'chart': {'type': 'line'},
                            'title': {'text': 'tps(pgbench_clients)'},
                            'xaxis': {'title': {'text': 'pgbench_clients'}},
                            'yaxis': {'title': {'text': 'TPS'}},
                        },
                    },
                },
            }
        },
    }


def extract_payload(html_text: str) -> dict:
    match = re.search(
        r'<script id="pg-perf-bench-report" type="application/json">'
        r'(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_render_html_is_monolithic_and_keeps_report_model() -> None:
    report = sample_report()

    html_text = render_html(report)

    assert extract_payload(html_text) == report
    assert 'echarts.init' in html_text
    assert 'window.hljs' in html_text
    assert 'pg-perf-bench-third-party-licenses' in html_text
    assert '__PAYLOAD__' not in html_text
    assert 'cdn.datatables.net' not in html_text
    assert 'cdnjs.cloudflare.com' not in html_text
    assert 'code.jquery.com' not in html_text
    assert '<script src=' not in html_text
    assert '<link rel="stylesheet" href=' not in html_text
    assert 'https://github.com/O2eg/pg_perf_bench' in html_text
    assert 'https://o2eg.com/' in html_text
    assert 'https://t.me/O2egg' in html_text
    assert (
        html_text.index('https://github.com/O2eg/pg_perf_bench')
        < html_text.index('https://o2eg.com/')
        < html_text.index('https://t.me/O2egg')
    )
    assert '<body class="nav-collapsed">' in html_text
    assert 'class="nav-toggle"' in html_text
    assert 'class="button nav-toggle"' not in html_text
    assert 'text-overflow: ellipsis;' in html_text
    assert 'white-space: nowrap;' in html_text
    assert 'class="report-toolbar"' in html_text
    assert 'class="filter-summary-panel"' in html_text
    assert 'class="page-scroll-controls"' in html_text
    assert 'id="scrollToTop"' in html_text
    assert 'id="scrollToBottom"' in html_text
    assert 'Showing ${visible} from ${renderedItems}' in html_text
    assert 'closeChartExportMenus' in html_text
    assert "event.target.closest('.chart-export-menu')" in html_text
    assert 'const DETAILS_ANIMATION_MS = 300;' in html_text
    assert 'const wrapDetailsContent' in html_text
    assert 'const bindAnimatedDetails' in html_text
    assert 'setDetailsOpen(item, true, true);' in html_text
    assert 'setDetailsOpen(item, false, true);' in html_text
    assert 'width: max-content;' in html_text
    assert 'max-height: 72vh;' in html_text
    assert 'const renderTableCell' in html_text
    assert "'Show more'" in html_text
    assert "'Show less'" in html_text
    assert 'class="theme-toggle header-theme-toggle"' in html_text
    assert html_text.index('header-theme-toggle') < html_text.index('<main class="shell">')
    assert "'chart-legend-panel'" in html_text
    assert "title: 'Zoom in'" in html_text
    assert "title: 'Zoom out'" in html_text
    assert "title: 'Export'" in html_text
    assert "['svg', 'png', 'csv']" in html_text
    assert "type: 'slider'" not in html_text
    assert 'parameters:' in html_text


def test_render_html_escapes_script_breakout_sequences() -> None:
    report = sample_report()
    attack = '</script><script>globalThis.reportWasCompromised = true</script>'
    report['description'] = attack

    html_text = render_html(report)

    assert attack not in html_text
    assert extract_payload(html_text)['description'] == attack


def test_save_report_writes_unchanged_json_and_monolithic_html(
    tmp_path: Path,
) -> None:
    report = sample_report()

    save_report(MagicMock(), report, str(tmp_path))

    json_path = tmp_path / 'smoke_report.json'
    html_path = tmp_path / 'smoke_report.html'
    assert json.loads(json_path.read_text(encoding='utf-8')) == report
    assert extract_payload(html_path.read_text(encoding='utf-8')) == report
