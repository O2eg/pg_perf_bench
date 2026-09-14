from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.const import BENCHMARK_TEMPLATE_JSON_PATH
from pg_perf_bench.join import ReportJoiner
from pg_perf_bench.pgbench_metrics import parse_pgbench_metrics
from pg_perf_bench.report.commands import chart_pgbench_metric
from pg_perf_bench.report.processing import get_report_structure

SUMMARY = """transaction type: multiple scripts
number of clients: 8
duration: 10 s
number of transactions actually processed: 80
number of failed transactions: 10 (10.000%)
number of transactions retried: 20 (20.000%)
number of transactions skipped: 10 (10.000%)
total number of retries: 50
latency average = 1.234 ms
latency stddev = 0.567 ms
initial connection time = 2.345 ms
tps = 8.765432 (without initial connection time)
SQL script 1: query.sql
 - number of failed transactions: 10 (50.000%)
 - number of transactions retried: 15 (75.000%)
 - latency average = 99.999 ms
 - latency stddev = 88.888 ms
"""


def _chart(metric='latency_stddev_ms'):
    report = get_report_structure(BENCHMARK_TEMPLATE_JSON_PATH)
    return report['sections']['result']['reports']['chart_' + metric]


def _data(values, metric='latency_stddev_ms'):
    return {
        'workload_conf': {
            'pgbench_iter_name': 'pgbench_clients',
            'pgbench_iter_list': list(range(1, len(values) + 1)),
        },
        'report_conf': {'report_name': 'example'},
        'benchmark_runs': [{'metrics': {metric: value}} for value in values],
    }


def test_overall_metrics_preserve_precision_and_pgbench_percentage_denominators():
    metrics = parse_pgbench_metrics(SUMMARY)
    assert metrics == {
        'clients': 8,
        'duration_seconds': 10,
        'transactions': 80,
        'latency_average_ms': 1.234,
        'latency_stddev_ms': 0.567,
        'initial_connection_time_ms': 2.345,
        'tps': 8.765432,
        'failed_transactions_percent': 10.0,
        'retried_transactions_percent': 20.0,
    }
    assert BenchmarkRunner.get_pgbench_results(SUMMARY) == [8, 10, 80, 1.234, 2.345, 8.765432]


def test_optional_summary_metrics_do_not_fall_back_to_script_or_connection_statistics():
    output = """latency average = 1.0 ms
average connection time = 2.0 ms
tps = 42.0 (including reconnection times)
SQL script 1: example.sql
 - latency stddev = 50.0 ms
 - number of failed transactions: 0 (0.000%)
 - number of transactions retried: 0 (0.000%)
"""
    metrics = parse_pgbench_metrics(output)
    for key in (
        'latency_stddev_ms',
        'initial_connection_time_ms',
        'failed_transactions_percent',
        'retried_transactions_percent',
    ):
        assert metrics[key] is None


def test_zero_is_distinct_from_unreported_and_decimal_comma_is_supported():
    metrics = parse_pgbench_metrics(
        'latency stddev = 0,125 ms\nnumber of failed transactions: 0 (0,000%)\n'
    )
    assert metrics['latency_stddev_ms'] == 0.125
    assert metrics['failed_transactions_percent'] == 0
    assert metrics['retried_transactions_percent'] is None


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '101', '9' * 400])
def test_invalid_percentages_are_missing(value):
    assert (
        parse_pgbench_metrics(f'number of failed transactions: 1 ({value}%)')[
            'failed_transactions_percent'
        ]
        is None
    )


@pytest.mark.parametrize(
    'metric',
    [
        'latency_average_ms',
        'latency_stddev_ms',
        'failed_transactions_percent',
        'retried_transactions_percent',
        'initial_connection_time_ms',
    ],
)
def test_each_chart_keeps_zero_missing_points_and_precision(metric):
    item = _chart(metric)
    chart_pgbench_metric(_data([0, None, 0.012345], metric), item)
    assert item['data']['series'][0]['data'] == [[1, 0], [2, None], [3, 0.012345]]
    assert item['collection_status'] == 'partial'
    assert item['data']['missing_data_message']


def test_chart_with_no_measurements_has_explicit_message_and_no_zero_values():
    item = _chart()
    chart_pgbench_metric(_data([None, float('nan'), True]), item)
    assert item['collection_status'] == 'empty'
    assert item['data']['missing_data_message'].startswith('No data.')
    assert all(point[1] is None for point in item['data']['series'][0]['data'])


def test_chart_preserves_duration_axis_and_can_read_legacy_average_latency():
    data = _data([1, 2], 'latency_average_ms')
    del data['benchmark_runs']
    data['workload_conf'] = {'pgbench_iter_name': 'pgbench_time', 'pgbench_iter_list': [5, 10]}
    data['pgbench_outputs'] = [[1, 5, 100, 0.123, 1, 100], [1, 10, 200, 0.456, 2, 100]]
    item = _chart('latency_average_ms')
    chart_pgbench_metric(data, item)
    assert item['data']['series'][0]['data'] == [[5, 0.123], [10, 0.456]]
    assert item['data']['xaxis']['title']['text'] == 'Duration [s]'


@pytest.mark.parametrize('presence', [(True, True), (False, True), (True, False)])
def test_join_keeps_optional_charts_and_missing_source_values(presence):
    reports = []
    for name, present, values in zip(
        ('left', 'right'), presence, ([0, None], [1.2, 3.4]), strict=True
    ):
        items = {
            'chart': {'data': {'series': [{'name': name, 'data': [[1, 10], [2, 20]]}]}},
            'pgbench_outputs': {'data': []},
        }
        if present:
            item = _chart()
            chart_pgbench_metric(_data(values), item)
            items['chart_latency_stddev_ms'] = item
        reports.append({'report_name': name, 'sections': {'result': {'reports': items}}})
    before = deepcopy(reports)
    joined = ReportJoiner.merge_reports(MagicMock(), ['left', 'right'], reports, [])
    item = joined['sections']['result']['reports']['chart_latency_stddev_ms']
    assert [series['name'] for series in item['data']['series']] == ['left', 'right']
    assert item['data']['series'][0]['data'] == [[1, 0 if presence[0] else None], [2, None]]
    assert item['data']['series'][1]['data'] == [
        [1, 1.2 if presence[1] else None],
        [2, 3.4 if presence[1] else None],
    ]
    assert reports == before
