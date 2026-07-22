import asyncio
from copy import deepcopy
from unittest.mock import MagicMock

from pg_perf_bench.const import BENCHMARK_TEMPLATE_JSON_PATH
from pg_perf_bench.join import ReportJoiner
from pg_perf_bench.report.processing import get_report_structure
from pg_perf_bench.system_metrics import (
    OS_METRIC_IDS,
    build_system_metrics_section,
    collect_system_metrics,
    infer_pgbench_duration,
)


def test_duration_is_inferred_from_modern_and_short_pgbench_options():
    assert infer_pgbench_duration('pgbench --time=60 db') == 60
    assert infer_pgbench_duration('pgbench --time 15 db') == 15
    assert infer_pgbench_duration('pgbench -T2 db') == 2
    assert infer_pgbench_duration('custom command', 3.5) == 3.5


def test_pg_diag_engine_builds_complete_os_metric_catalog():
    result = asyncio.run(
        collect_system_metrics(
            connection_type='local',
            connection=object(),
            duration_seconds=0.25,
            interval_seconds=0.1,
        )
    )
    assert tuple(result['charts']) == OS_METRIC_IDS
    assert result['engine']['name'] == 'pg_diag'
    assert result['charts']['os.cpu_utilization']['pg_diag_result']['kind'] == 'chart'
    assert result['charts']['os.memory_usage']['pg_diag_result']['series']


def _report(name: str, tps: float, marker: int) -> dict:
    report = get_report_structure(BENCHMARK_TEMPLATE_JSON_PATH)
    report['artifact_schema_version'] = 'pg_perf_bench/report-v1'
    report['report_name'] = name
    report['sections']['result']['reports']['chart']['data']['series'] = [
        {'name': 'tps', 'data': [[1, tps]]}
    ]
    report['sections']['result']['reports']['pgbench_outputs']['data'] = [[1, 1, marker, 1, 1, tps]]
    metric_result = {
        metric_id: {
            'title': metric_id,
            'sampler': 'os.cpu',
            'data': {'series': [{'name': 'value', 'data': [[marker, marker]]}]},
        }
        for metric_id in OS_METRIC_IDS
    }
    runs = [
        {
            'iteration': {'parameter': 'pgbench_clients', 'value': 1},
            'system_metrics': {
                'charts': metric_result,
                'collection_scope': 'local_docker_host',
                'errors': [],
            },
        }
    ]
    report['sections']['os_metrics'] = build_system_metrics_section(runs)
    return report


def test_join_stacks_cpu_charts_vertically_per_source_report():
    left = _report('baseline', 10.0, 1)
    right = _report('tuned', 20.0, 2)
    joined = ReportJoiner.merge_reports(
        MagicMock(),
        ['baseline.json', 'tuned.json'],
        [deepcopy(left), deepcopy(right)],
        [],
    )
    assert joined is not None
    blocks = joined['sections']['os_metrics']['reports']['os_cpu_utilization']['data']
    assert [block['report_name'] for block in blocks] == ['baseline', 'tuned']
    assert [block['chart']['series'][0]['data'][0][1] for block in blocks] == [1, 2]
