"""Artifact validation and deterministic summaries for pg_play orchestration."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from pg_perf_bench.contracts import ARTIFACT_SCHEMA_VERSION, canonical_hash, file_hash
from pg_perf_bench.errors import ReportError


def validate_artifact(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ReportError('benchmark artifact root must be a JSON object')
    if document.get('artifact_schema_version') != ARTIFACT_SCHEMA_VERSION:
        raise ReportError(
            f'unsupported benchmark artifact schema: {document.get("artifact_schema_version")!r}'
        )
    if not isinstance(document.get('report_name'), str) or not document['report_name']:
        raise ReportError('benchmark artifact report_name must be a non-empty string')
    if not isinstance(document.get('sections'), dict):
        raise ReportError('benchmark artifact sections must be an object')
    runs = document.get('benchmark_runs')
    if runs is not None and not isinstance(runs, list):
        raise ReportError('benchmark_runs must be an array when present')
    return document


def load_artifact(path: str | Path) -> dict[str, Any]:
    artifact_path = Path(path).expanduser().resolve()
    try:
        document = json.loads(artifact_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f'cannot read benchmark artifact {artifact_path}: {exc}') from exc
    return validate_artifact(document)


def artifact_descriptor(
    path: str | Path, *, kind: str, schema_version: str | None
) -> dict[str, Any]:
    artifact_path = Path(path).expanduser().resolve()
    return {
        'kind': kind,
        'schema_version': schema_version,
        'path': str(artifact_path),
        'hash': file_hash(artifact_path),
        'size_bytes': artifact_path.stat().st_size,
    }


def summarize_artifact(document: dict[str, Any]) -> dict[str, Any]:
    artifact = validate_artifact(document)
    statuses: Counter[str] = Counter()
    item_count = 0
    for section in artifact['sections'].values():
        reports = section.get('reports') if isinstance(section, dict) else None
        if not isinstance(reports, dict):
            continue
        for item in reports.values():
            item_count += 1
            if isinstance(item, dict):
                statuses[str(item.get('collection_status', 'unknown'))] += 1
            else:
                statuses['unknown'] += 1

    runs = artifact.get('benchmark_runs') or []
    iteration_parameters = sorted(
        {
            str(run.get('iteration', {}).get('parameter'))
            for run in runs
            if isinstance(run, dict) and run.get('iteration', {}).get('parameter')
        }
    )
    iteration_values = [
        run.get('iteration', {}).get('value')
        for run in runs
        if isinstance(run, dict) and isinstance(run.get('iteration'), dict)
    ]
    tps_values = [
        run.get('metrics', {}).get('tps')
        for run in runs
        if isinstance(run, dict) and isinstance(run.get('metrics'), dict)
    ]
    valid_tps_runs = [
        run
        for run in runs
        if isinstance(run, dict)
        and isinstance(run.get('metrics'), dict)
        and isinstance(run['metrics'].get('tps'), (int, float))
        and not isinstance(run['metrics'].get('tps'), bool)
    ]
    maximum_tps = artifact.get('maximum_tps')
    if maximum_tps is None and valid_tps_runs:
        best = max(valid_tps_runs, key=lambda run: float(run['metrics']['tps']))
        maximum_tps = {
            'tps': best['metrics']['tps'],
            'iteration': best.get('iteration'),
            'metrics': best['metrics'],
        }
    return {
        'schema_version': 'pg_perf_bench/summary-v1',
        'artifact_schema_version': artifact['artifact_schema_version'],
        'artifact_hash': canonical_hash(artifact),
        'report_name': artifact['report_name'],
        'generator': artifact.get('generator'),
        'collection_summary': artifact.get('collection_summary'),
        'item_count': item_count,
        'collection_statuses': dict(sorted(statuses.items())),
        'benchmark_run_count': len(runs),
        'iteration_parameters': iteration_parameters,
        'iteration_values': iteration_values,
        'tps_values': tps_values,
        'maximum_tps': maximum_tps,
        'benchmark_methodology': artifact.get('benchmark_methodology'),
        'postgresql_compatibility': artifact.get('postgresql_compatibility'),
        'workload_evidence': {
            key: (artifact.get('workload_evidence') or {}).get(key)
            for key in (
                'schema_version',
                'profile_id',
                'definition_hash',
                'execution_hash',
            )
        },
        'database_configuration_evidence': artifact.get('database_configuration_evidence'),
        'environment_evidence': artifact.get('environment_evidence'),
        'system_metric_ids': sorted(
            str(name)
            for name in (
                (artifact.get('sections', {}).get('os_metrics', {}).get('reports', {})) or {}
            )
        ),
    }
