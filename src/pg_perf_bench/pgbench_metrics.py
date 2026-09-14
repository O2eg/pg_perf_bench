"""Parse the overall pgbench summary without mixing in per-script statistics."""

from __future__ import annotations

import math
import re

LEGACY_METRIC_KEYS = (
    'clients',
    'duration_seconds',
    'transactions',
    'latency_average_ms',
    'initial_connection_time_ms',
    'tps',
)


def parse_pgbench_metrics(output: str) -> dict[str, int | float | None]:
    summary = re.split(r'(?m)^[ \t]*SQL script \d+:', output, maxsplit=1)[0]
    number = r'(\d+(?:[.,]\d+)?)'

    def value(pattern: str, *, integer: bool = False, percent: bool = False):
        match = re.search(r'^[ \t]*' + pattern, summary, re.MULTILINE)
        if match is None:
            return None
        if integer:
            return int(match.group(1))
        result = float(match.group(1).replace(',', '.'))
        if not math.isfinite(result) or (percent and not 0 <= result <= 100):
            return None
        return result

    return {
        'clients': value(r'number of clients:[ \t]*(\d+)', integer=True),
        'duration_seconds': value(r'duration:[ \t]*(\d+)', integer=True),
        'transactions': value(
            r'number of transactions actually processed:[ \t]*(\d+)', integer=True
        ),
        'latency_average_ms': value(r'latency average[ \t]*=[ \t]*' + number + r'[ \t]+ms'),
        'initial_connection_time_ms': value(
            r'initial connection time[ \t]*=[ \t]*' + number + r'[ \t]+ms'
        ),
        'tps': value(r'tps[ \t]*=[ \t]*' + number),
        'latency_stddev_ms': value(r'latency stddev[ \t]*=[ \t]*' + number + r'[ \t]+ms'),
        'failed_transactions_percent': value(
            r'number of failed transactions:[ \t]*\d+[ \t]+\([ \t]*' + number + r'[ \t]*%\)',
            percent=True,
        ),
        'retried_transactions_percent': value(
            r'number of transactions retried:[ \t]*\d+[ \t]+\([ \t]*' + number + r'[ \t]*%\)',
            percent=True,
        ),
    }
