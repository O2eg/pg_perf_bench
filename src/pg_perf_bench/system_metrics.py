"""pg_diag-backed operating-system sampling during benchmark workloads."""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pg_diag
from pg_diag.content_loader import load_content
from pg_diag.host_access import HostAccess, LocalHostAccess
from pg_diag.metric_engine import build_chart_result
from pg_diag.sampler_runtime import SamplerCollection, collect_sampler_providers
from pg_diag.ssh_transport import SshCommandResult

from pg_perf_bench.const import ConnectionType
from pg_perf_bench.errors import ConfigurationError

OS_METRIC_IDS = (
    'os.cpu_utilization',
    'os.cpu_load',
    'os.memory_usage',
    'os.memory_pressure',
    'os.disk_read_throughput',
    'os.disk_write_throughput',
    'os.disk_iops',
    'os.disk_utilization',
    'os.disk_latency',
    'os.network_receive_throughput',
    'os.network_transmit_throughput',
    'os.network_packets',
)
OS_SAMPLER_OUTPUTS = {'os.cpu', 'os.memory', 'os.disk', 'os.network'}
_PGBENCH_TIME_RE = re.compile(r'(?:^|\s)(?:--time(?:=|\s+)|-T(?:\s+)?)(\d+(?:\.\d+)?)(?=\s|$)')


class _SshConnectionHostAccess(HostAccess):
    """Use pg_perf_bench's established SSH session as a pg_diag host."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def run_script(
        self,
        script: str,
        *,
        arguments: tuple[str, ...] = (),
        timeout: float = 30.0,
    ) -> SshCommandResult:
        client = getattr(self.connection, 'client', None)
        if client is None:
            raise ConnectionError('SSH client is not initialized for system sampling')
        command = 'LC_ALL=C LANG=C /bin/sh -s -- ' + ' '.join(
            shlex.quote(value) for value in arguments
        )
        try:
            result = await asyncio.wait_for(
                client.run(command, input=script, check=False),
                timeout=float(timeout),
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f'remote sampler timed out after {timeout:g} seconds') from exc
        return SshCommandResult(
            int(result.exit_status),
            str(result.stdout or ''),
            str(result.stderr or ''),
        )


def infer_pgbench_duration(command: str, override: float | None = None) -> float:
    if override is not None:
        return float(override)
    matches = _PGBENCH_TIME_RE.findall(command)
    if len(matches) != 1:
        raise ConfigurationError(
            'system metric sampling requires exactly one pgbench --time/-T option; '
            'add it to --workload-command or pass --system-metrics-duration'
        )
    duration = float(matches[0])
    if duration <= 0:
        raise ConfigurationError('pgbench sampling duration must be greater than zero')
    return duration


def _content_path() -> Path:
    return Path(pg_diag.__file__).resolve().parent / 'content'


def _host_access(connection_type: str, connection: Any) -> tuple[HostAccess, str]:
    if connection_type == str(ConnectionType.SSH):
        return _SshConnectionHostAccess(connection), 'remote_database_host'
    if connection_type == str(ConnectionType.DOCKER):
        return LocalHostAccess(), 'local_docker_host'
    return LocalHostAccess(), 'local_database_host'


def _echarts_data(metric: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    chart = result.get('chart') or {}
    return {
        'title': {'text': metric.get('title') or ''},
        'chart': {
            'type': 'line',
            'kind': chart.get('kind', 'line'),
            'unit': chart.get('unit'),
            'quantity': chart.get('quantity'),
        },
        'xaxis': {'type': 'datetime', 'title': {'text': 'Time'}},
        'yaxis': {'title': {'text': chart.get('unit') or ''}},
        'series': [
            {
                'name': series.get('name'),
                'unit': series.get('unit'),
                'color': series.get('color'),
                'data': [
                    [point.get('t'), point.get('value')] for point in series.get('points') or []
                ],
            }
            for series in result.get('series') or []
        ],
    }


async def collect_system_metrics(
    *,
    connection_type: str,
    connection: Any,
    duration_seconds: float,
    interval_seconds: float,
    stop_event: asyncio.Event | None = None,
    duration_limit_seconds: float | None = None,
) -> dict[str, Any]:
    """Keep completed provider windows, including the workload's draining period.

    Providers return a complete window rather than incremental samples. Finish the
    current interval on stop (also on workload failure) instead of cancelling it
    and losing its data. Size/diagnostic queries must wait for this final window.
    """
    content = load_content(_content_path())
    host, collection_scope = _host_access(connection_type, connection)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    collection = SamplerCollection(samples={}, errors=[])
    reason = 'duration'
    if stop_event is None:
        collection = await collect_sampler_providers(
            content, host, duration_seconds, interval_seconds, set(OS_SAMPLER_OUTPUTS)
        )
    else:

        async def collect_provider(outputs: set[str]) -> None:
            nonlocal reason
            while not stop_event.is_set():
                window = interval_seconds
                if duration_limit_seconds is not None:
                    remaining = duration_limit_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    window = min(window, remaining)
                try:
                    part = await collect_sampler_providers(
                        content, host, window, interval_seconds, outputs
                    )
                except Exception as exc:
                    collection.errors.extend(
                        {'sampler': name, 'message': str(exc)} for name in sorted(outputs)
                    )
                    reason = 'collector_error'
                    break
                for name, samples in part.samples.items():
                    if name in outputs:
                        collection.samples.setdefault(name, []).extend(samples)
                collection.errors.extend(part.errors)
                if not any(part.samples.values()):
                    reason = 'collector_error'
                    break

        # pg_diag's Linux provider joins /proc and iostat internally. Select
        # their outputs separately so iostat's cadence cannot stall /proc.
        tasks = [
            asyncio.create_task(collect_provider(outputs), name=f'os-provider:{name}')
            for name, outputs in (
                ('proc', OS_SAMPLER_OUTPUTS - {'os.disk'}),
                ('disk', {'os.disk'}),
            )
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if stop_event.is_set():
            reason = 'workload_finished'
    charts: dict[str, Any] = {}
    for metric_id in OS_METRIC_IDS:
        metric = content.metrics[metric_id]
        sampler_id = str(metric['source_sampler'])
        result = build_chart_result(metric, collection.samples.get(sampler_id, []), {})
        charts[metric_id] = {
            'metric_id': metric_id,
            'title': metric['title'],
            'sampler': sampler_id,
            'data': _echarts_data(metric, result),
            'pg_diag_result': result,
        }
    return {
        'schema_version': 'pg_perf_bench/system-metrics-v1',
        'engine': {'name': 'pg_diag', 'version': pg_diag.__version__},
        'collection_scope': collection_scope,
        'duration_seconds': time.monotonic() - started,
        'requested_duration_seconds': float(duration_seconds),
        'duration_limit_seconds': duration_limit_seconds,
        'started_at': started_at,
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'stop_reason': reason,
        'interval_seconds': float(interval_seconds),
        'samples': collection.samples,
        'errors': collection.errors,
        'charts': charts,
    }


def build_system_metrics_section(benchmark_runs: list[dict[str, Any]]) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for metric_id in OS_METRIC_IDS:
        blocks = []
        title = metric_id
        for run in benchmark_runs:
            system_metrics = run.get('system_metrics') or {}
            metric = (system_metrics.get('charts') or {}).get(metric_id)
            if not metric:
                continue
            title = metric.get('title') or title
            blocks.append(
                {
                    'iteration': run.get('iteration'),
                    'iteration_status': run.get('status', 'completed'),
                    'collection_scope': system_metrics.get('collection_scope'),
                    'duration_seconds': system_metrics.get('duration_seconds'),
                    'interval_seconds': system_metrics.get('interval_seconds'),
                    'errors': [
                        error
                        for error in system_metrics.get('errors') or []
                        if error.get('sampler') == metric.get('sampler')
                    ],
                    'chart': metric.get('data'),
                }
            )
        errors = [
            error
            for run in benchmark_runs
            for error in (run.get('system_metrics') or {}).get('errors') or []
            if isinstance(error, dict)
            and error.get('sampler') in {'os', content_sampler(metric_id)}
        ]
        reports[metric_id.replace('.', '_')] = {
            'header': title,
            'description': (
                'Collected during the measured pgbench window by the pg_diag Linux sampler '
                'and metric engine.'
            ),
            'state': (
                'expanded'
                if metric_id
                in {
                    'os.cpu_utilization',
                    'os.cpu_load',
                    'os.memory_usage',
                    'os.disk_iops',
                    'os.disk_utilization',
                    'os.disk_latency',
                }
                else 'collapsed'
            ),
            'item_type': 'chart_group',
            'data': blocks,
            'collection_status': 'partial' if errors else 'ok' if blocks else 'empty',
            'reason': '; '.join(sorted({str(error.get('message') or '') for error in errors})),
        }
    return {
        'header': 'Operating system metrics during benchmark',
        'description': (
            'CPU, RAM, disk, and network timelines collected concurrently with each workload '
            'iteration using pg_diag.'
        ),
        'state': 'expanded',
        'reports': reports,
    }


def content_sampler(metric_id):
    """Resolve error scope even when collection failed before charts were built."""
    return {
        'cpu': 'os.cpu',
        'memory': 'os.memory',
        'disk': 'os.disk',
        'network': 'os.network',
    }[metric_id.removeprefix('os.').split('_', 1)[0]]
