"""Stable runtime, artifact, and secret-handling contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONTRACT_VERSION = 'pg_play/component/v1'
ARTIFACT_SCHEMA_VERSION = 'pg_perf_bench/report-v1'

EXIT_CODES = {
    'success': 0,
    'validation_error': 2,
    'precondition_failed': 3,
    'partial': 5,
    'execution_error': 6,
    'cancelled': 7,
}

SECRET_FIELDS = frozenset(
    {
        'password',
        'pg_password',
        'workload_password',
        'admin_password',
        'ssh_key_passphrase',
        'dsn',
    }
)


def is_secret_field(name: object) -> bool:
    normalized = str(name).strip().lower()
    return normalized in SECRET_FIELDS or normalized.endswith('_password')


def redact_text(value: object, secrets: list[str | None] | tuple[str | None, ...]) -> str:
    redacted = str(value)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(str(secret), '***')
    return redacted


def redact_mapping(
    value: Mapping[str, Any],
    *,
    secrets: list[str | None] | tuple[str | None, ...] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if is_secret_field(key):
            result[str(key)] = '***' if item not in (None, '') else item
        elif isinstance(item, Mapping):
            result[str(key)] = redact_mapping(item, secrets=secrets)
        elif isinstance(item, (list, tuple)):
            result[str(key)] = [
                redact_mapping(entry, secrets=secrets)
                if isinstance(entry, Mapping)
                else redact_text(entry, secrets)
                if isinstance(entry, str)
                else entry
                for entry in item
            ]
        elif isinstance(item, str):
            result[str(key)] = redact_text(item, secrets)
        else:
            result[str(key)] = item
    return result


def envelope(
    command: str,
    status: str,
    *,
    component_version: str,
    request_id: str | None = None,
    result: Any = None,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        'contract_version': CONTRACT_VERSION,
        'component': 'pg_perf_bench',
        'component_version': component_version,
        'command': command,
        'request_id': request_id,
        'status': status,
        'result': result,
        'artifacts': artifacts or [],
        'warnings': warnings or [],
        'error': error,
    }
