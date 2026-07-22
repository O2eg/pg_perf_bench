import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pg_perf_bench.join import ReportJoiner


def _report(name, fact, tps):
    return {
        'artifact_schema_version': 'pg_perf_bench/report-v1',
        'report_name': name,
        'benchmark_runs': [{'iteration': {'index': 1}, 'metrics': {'tps': tps}}],
        'sections': {
            'db': {
                'reports': {
                    'fact': {
                        'header': 'Fact',
                        'item_type': 'plain_text',
                        'sql_command_file': 'full_version.sql',
                        'data': fact,
                    }
                }
            },
            'result': {
                'reports': {
                    'chart': {'data': {'series': [{'name': '', 'data': [[1, tps]]}]}},
                    'pgbench_outputs': {'data': [[1, 10, 1, 1, 1, tps]]},
                }
            },
        },
    }


def test_merge_always_compares_against_unmodified_reference():
    reports = [
        _report('reference', 'A', 10),
        _report('second', 'B', 20),
        _report('third', 'C', 30),
    ]
    original = deepcopy(reports[0])
    merged = ReportJoiner.merge_reports(
        MagicMock(),
        ['reference.json', 'second.json', 'third.json'],
        reports,
        [],
    )

    assert reports[0] == original
    fact = merged['sections']['db']['reports']['fact']
    assert fact['data'] == [
        ['reference', 'A'],
        ['second', 'B'],
        ['third', 'C'],
    ]
    assert fact['item_type'] == 'table'
    assert fact['theader'] == ['report', 'value']
    outputs = merged['sections']['result']['reports']['pgbench_outputs']['data']
    assert all(len(row) == 2 for row in outputs)
    assert [entry['report_name'] for entry in merged['joined_benchmark_runs']] == [
        'reference',
        'second',
        'third',
    ]
    assert [entry['maximum_tps']['tps'] for entry in merged['joined_maximum_tps']] == [
        10,
        20,
        30,
    ]


def test_required_comparison_item_must_exist():
    left = _report('left', 'A', 10)
    right = _report('right', 'A', 10)
    with pytest.raises(ValueError, match='Comparison item is missing'):
        ReportJoiner.compare_reports(MagicMock(), left, right, ['sections.db.reports.missing.data'])


def test_machine_join_can_surface_the_exact_controlled_dimension_mismatch():
    left = _report('left', 'A', 10)
    right = _report('right', 'B', 11)

    with pytest.raises(ValueError, match='Required comparison item differs: sections.db'):
        ReportJoiner.merge_reports(
            MagicMock(),
            ['left.json', 'right.json'],
            [left, right],
            ['sections.db.reports.fact.data'],
            raise_on_error=True,
        )


def test_invalid_explicit_reference_is_not_silently_replaced(tmp_path):
    (tmp_path / 'reference.json').write_text('{broken', encoding='utf-8')
    (tmp_path / 'other.json').write_text(json.dumps(_report('other', 'A', 10)), encoding='utf-8')

    loaded = ReportJoiner.load_reports(MagicMock(), str(tmp_path), 'reference.json')

    assert loaded is None


def test_report_name_containing_join_is_loaded(tmp_path):
    source = _report('joined-name', 'A', 10)
    (tmp_path / 'contains-join-word.json').write_text(json.dumps(source), encoding='utf-8')

    loaded = ReportJoiner.load_reports(
        MagicMock(),
        str(tmp_path),
        'contains-join-word.json',
    )

    assert loaded is not None
    assert loaded[0] == ['contains-join-word.json']


def test_explicit_report_paths_do_not_load_unrelated_json(tmp_path):
    first = tmp_path / 'pg18-baseline.json'
    second = tmp_path / 'pg18-tuned.json'
    first.write_text(json.dumps(_report('pg18-baseline', 'A', 10)), encoding='utf-8')
    second.write_text(json.dumps(_report('pg18-tuned', 'A', 20)), encoding='utf-8')
    (tmp_path / 'state.json').write_text('{"state": "not-a-report"}', encoding='utf-8')

    loaded = ReportJoiner.load_report_paths(
        MagicMock(),
        [str(second), str(first)],
        first.name,
    )

    assert loaded is not None
    assert loaded[0] == [first.name, second.name]


def test_merge_rejects_incomplete_result_structure():
    reference = _report('reference', 'A', 10)
    incomplete = _report('incomplete', 'A', 20)
    del incomplete['sections']['result']['reports']['chart']

    assert (
        ReportJoiner.merge_reports(
            MagicMock(),
            ['reference.json', 'incomplete.json'],
            [reference, incomplete],
            [],
        )
        is None
    )


def test_merge_rejects_incompatible_artifact_schema():
    reference = _report('reference', 'A', 10)
    incompatible = _report('incompatible', 'A', 20)
    incompatible['artifact_schema_version'] = 'pg_perf_bench/report-v2'

    assert (
        ReportJoiner.merge_reports(
            MagicMock(),
            ['reference.json', 'incompatible.json'],
            [reference, incompatible],
            [],
        )
        is None
    )


def test_join_rebases_report_local_log_links_to_join_directory(tmp_path: Path):
    source_report = tmp_path / 'source' / 'baseline.json'
    destination = tmp_path / 'joined'
    report = _report('baseline', 'A', 10)
    report['sections']['result']['reports']['logs'] = {
        'item_type': 'link',
        'data': 'db_logs/baseline.tar.gz',
    }

    ReportJoiner.rebase_log_links(report, source_report, destination)

    expected = os.path.relpath(
        source_report.parent / 'db_logs' / 'baseline.tar.gz',
        destination,
    )
    assert report['sections']['result']['reports']['logs']['data'] == expected
