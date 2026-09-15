"""Managed PostgreSQL metadata and explicit limits of server-side collection."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from pg_perf_bench.errors import ConfigurationError

MANAGED_NO_DATA = 'No data. Managed PostgreSQL.'


def read_managed_pg_info(value: str | Path) -> dict[str, Any]:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError(f'--managed-pg-info must be an existing regular file: {path}')
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f'cannot read --managed-pg-info: {path}: {exc}') from exc
    try:
        content = data.decode('utf-8')
        encoding = 'utf-8'
        if '\x00' in content:
            raise UnicodeError('binary data')
    except UnicodeError:
        content = base64.b64encode(data).decode('ascii')
        encoding = 'base64'
    return {
        'schema_version': 'pg_perf_bench/managed-pg-info-v1',
        'name': path.name,
        'path': str(path),
        'media_type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
        'encoding': encoding,
        'size_bytes': len(data),
        'hash': 'sha256:' + hashlib.sha256(data).hexdigest(),
        'content': content,
    }


def mark_managed_unavailable(item: dict[str, Any]) -> None:
    for key in ('shell_command_file', 'sql_command_file', 'python_command', 'theader'):
        item.pop(key, None)
    item.update(
        item_type='plain_text',
        data=MANAGED_NO_DATA,
        collection_status='unsupported',
        reason=MANAGED_NO_DATA,
    )


def add_managed_report_metadata(report: dict[str, Any], metadata: dict[str, Any]) -> None:
    report['managed_pg_info'] = metadata
    description = 'Instance metadata supplied with --managed-pg-info.'
    if metadata['encoding'] == 'base64':
        description += ' Binary file content is shown as Base64.'
    report['sections']['db']['reports']['managed_pg_info'] = {
        'header': 'Managed PostgreSQL instance',
        'description': description,
        'state': 'expanded',
        'item_type': 'plain_text',
        'data': metadata['content'],
        'collection_status': 'ok',
    }
    mark_managed_report_unavailable(report)


def mark_managed_report_unavailable(report: dict[str, Any]) -> None:
    report['managed_postgresql'] = True
    for name, section in report['sections'].items():
        for item in section['reports'].values():
            if name == 'system' or 'shell_command_file' in item:
                mark_managed_unavailable(item)
    logs = report['sections']['result']['reports'].setdefault(
        'logs', {'header': 'PostgreSQL logs', 'state': 'collapsed'}
    )
    mark_managed_unavailable(logs)
