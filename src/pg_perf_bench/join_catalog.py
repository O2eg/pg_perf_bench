"""Packaged JOIN scenario catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pg_perf_bench.const import JOIN_TASKS_PATH

JOIN_TASK_SCHEMA_VERSION = 'pg_perf_bench/join-task-v1'
_JOIN_TASK_ID = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
_DOTTED_PATH = re.compile(r'^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$')

# Keep the only historic public name working while storing every canonical task
# in one directory with its own documentation.
LEGACY_JOIN_TASK_ALIASES = {
    'task_compare_dbs_on_single_host.json': 'optimize-db-config',
}


def normalize_join_task_id(identifier: str) -> str:
    """Return a canonical scenario id without allowing filesystem traversal."""
    value = LEGACY_JOIN_TASK_ALIASES.get(identifier, identifier)
    if value.endswith('.json'):
        value = value[:-5]
    if not _JOIN_TASK_ID.fullmatch(value):
        raise ValueError(
            f'invalid join task {identifier!r}; use a scenario id from the JOIN catalog'
        )
    return value


def join_task_path(identifier: str) -> Path:
    task_id = normalize_join_task_id(identifier)
    return JOIN_TASKS_PATH / task_id / 'task.json'


def validate_join_task(task: Any, *, expected_id: str | None = None) -> list[str]:
    """Validate one task definition and return human-readable errors."""
    errors: list[str] = []
    if not isinstance(task, dict):
        return ['task must be a JSON object']
    if task.get('schema_version') != JOIN_TASK_SCHEMA_VERSION:
        errors.append(f'schema_version must be {JOIN_TASK_SCHEMA_VERSION!r}')
    task_id = task.get('id')
    if not isinstance(task_id, str) or not _JOIN_TASK_ID.fullmatch(task_id):
        errors.append('id must be a lowercase kebab-case scenario id')
    elif expected_id is not None and task_id != expected_id:
        errors.append(f'id must match directory name {expected_id!r}')
    for field in ('title', 'purpose'):
        if not isinstance(task.get(field), str) or not task[field].strip():
            errors.append(f'{field} must be a non-empty string')
    for field in ('controlled_dimensions', 'variable_dimensions'):
        value = task.get(field)
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            errors.append(f'{field} must be a non-empty list of strings')
    items = task.get('items')
    if (
        not isinstance(items, list)
        or not items
        or not all(isinstance(item, str) and _DOTTED_PATH.fullmatch(item) for item in items)
    ):
        errors.append('items must be a non-empty list of dotted report paths')
    elif len(items) != len(set(items)):
        errors.append('items must not contain duplicates')
    return errors


def load_join_task(identifier: str) -> dict[str, Any]:
    """Load and validate a canonical packaged JOIN task."""
    task_id = normalize_join_task_id(identifier)
    path = join_task_path(task_id)
    if not path.is_file():
        raise FileNotFoundError(f'join task not found: {task_id}')
    try:
        task = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'cannot parse join task {task_id}: {exc}') from exc
    errors = validate_join_task(task, expected_id=task_id)
    if errors:
        raise ValueError(f'invalid join task {task_id}: ' + '; '.join(errors))
    return task


def join_task_catalog() -> list[dict[str, Any]]:
    """Return deterministic user-facing metadata for all packaged scenarios."""
    result: list[dict[str, Any]] = []
    for path in sorted(JOIN_TASKS_PATH.glob('*/task.json')):
        task = load_join_task(path.parent.name)
        result.append(
            {
                key: task[key]
                for key in (
                    'id',
                    'title',
                    'purpose',
                    'controlled_dimensions',
                    'variable_dimensions',
                )
            }
        )
    return result


def validate_join_task_catalog() -> list[str]:
    errors: list[str] = []
    for stray in sorted(JOIN_TASKS_PATH.glob('*.json')):
        errors.append(f'{stray}: flat JOIN definitions are not allowed; use SCENARIO_ID/task.json')
    paths = sorted(JOIN_TASKS_PATH.glob('*/task.json'))
    if not paths:
        return [f'No join task definitions found in {JOIN_TASKS_PATH}']
    seen: set[str] = set()
    for path in paths:
        task_id = path.parent.name
        if task_id in seen:
            errors.append(f'{path}: duplicate task id {task_id}')
        seen.add(task_id)
        if not (path.parent / 'README.md').is_file():
            errors.append(f'{path.parent}: missing README.md')
        try:
            task = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f'{path}: {exc}')
            continue
        errors.extend(f'{path}: {error}' for error in validate_join_task(task, expected_id=task_id))
    if not (JOIN_TASKS_PATH / 'README.md').is_file():
        errors.append(f'{JOIN_TASKS_PATH}: missing catalog README.md')
    return errors
