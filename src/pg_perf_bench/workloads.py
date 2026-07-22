"""Bundled workload profiles and portable workload evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pg_perf_bench.const import WORKLOAD_PROFILES_PATH
from pg_perf_bench.contracts import canonical_hash, file_hash

WORKLOAD_PROFILE_SCHEMA_VERSION = 'pg_perf_bench/workload-profile-v1'
WORKLOAD_EVIDENCE_SCHEMA_VERSION = 'pg_perf_bench/workload-evidence-v1'
WORKLOAD_CATALOG_SCHEMA_VERSION = 'pg_perf_bench/workload-catalog-v1'
_SOURCE_SUFFIXES = frozenset({'.py', '.sql'})
_MAX_SOURCE_BYTES = 5 * 1024 * 1024


def bundled_profile_names() -> tuple[str, ...]:
    return tuple(path.parent.name for path in sorted(WORKLOAD_PROFILES_PATH.glob('*/profile.json')))


def _safe_relative_path(value: object) -> Path:
    path = Path(str(value))
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError(f'unsafe workload asset path: {value!r}')
    return path


def validate_workload_profile(profile: Any, *, expected_id: str | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ['profile must be a JSON object']
    if profile.get('schema_version') != WORKLOAD_PROFILE_SCHEMA_VERSION:
        errors.append(f'schema_version must be {WORKLOAD_PROFILE_SCHEMA_VERSION!r}')
    profile_id = profile.get('id')
    if not isinstance(profile_id, str) or not profile_id:
        errors.append('id must be a non-empty string')
    elif expected_id is not None and profile_id != expected_id:
        errors.append(f'id must match directory name {expected_id!r}')
    for field in ('title', 'description'):
        if not isinstance(profile.get(field), str) or not profile[field].strip():
            errors.append(f'{field} must be a non-empty string')
    files = profile.get('files')
    required_roles = ('schema', 'generators', 'queries')
    if not isinstance(files, dict):
        errors.append('files must be an object')
    else:
        for role in required_roles:
            values = files.get(role)
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(item, str) and item for item in values)
            ):
                errors.append(f'files.{role} must be a non-empty list')
        all_paths = [
            item for values in files.values() if isinstance(values, list) for item in values
        ]
        if len(all_paths) != len(set(all_paths)):
            errors.append('workload asset paths must not be duplicated')
        for value in all_paths:
            try:
                _safe_relative_path(value)
            except ValueError as exc:
                errors.append(str(exc))
    benchmark = profile.get('benchmark')
    if not isinstance(benchmark, dict):
        errors.append('benchmark must be an object')
    else:
        for field in ('init_command', 'workload_command'):
            if not isinstance(benchmark.get(field), str) or not benchmark[field].strip():
                errors.append(f'benchmark.{field} must be a non-empty string')
        duration = benchmark.get('default_duration_seconds')
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            errors.append('benchmark.default_duration_seconds must be a positive integer')
    return errors


def load_workload_profile(profile_id: str) -> dict[str, Any]:
    if profile_id not in bundled_profile_names():
        raise ValueError(
            f'unknown workload profile {profile_id!r}; available: '
            + ', '.join(bundled_profile_names())
        )
    path = WORKLOAD_PROFILES_PATH / profile_id / 'profile.json'
    try:
        profile = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'cannot parse workload profile {profile_id}: {exc}') from exc
    errors = validate_workload_profile(profile, expected_id=profile_id)
    if errors:
        raise ValueError(f'invalid workload profile {profile_id}: ' + '; '.join(errors))
    return profile


def workload_profile_catalog() -> dict[str, Any]:
    profiles = []
    for profile_id in bundled_profile_names():
        profile = load_workload_profile(profile_id)
        profiles.append(
            {
                'id': profile['id'],
                'title': profile['title'],
                'description': profile['description'],
                'requires_write': bool(profile.get('requires_write')),
                'default_scale': profile.get('default_scale'),
                'default_duration_seconds': profile['benchmark']['default_duration_seconds'],
                'query_count': len(profile['files']['queries']),
            }
        )
    return {'schema_version': WORKLOAD_CATALOG_SCHEMA_VERSION, 'profiles': profiles}


def validate_workload_profiles() -> list[str]:
    errors: list[str] = []
    profile_ids = bundled_profile_names()
    if not profile_ids:
        return [f'No workload profiles found in {WORKLOAD_PROFILES_PATH}']
    if not (WORKLOAD_PROFILES_PATH / 'README.md').is_file():
        errors.append(f'{WORKLOAD_PROFILES_PATH}: missing README.md')
    for profile_id in profile_ids:
        root = WORKLOAD_PROFILES_PATH / profile_id
        if not (root / 'README.md').is_file():
            errors.append(f'{root}: missing README.md')
        if (root / 'profile.yml').exists():
            errors.append(f'{root}: profile.yml belongs to pg_workload and must not be bundled')
        path = root / 'profile.json'
        try:
            profile = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f'{path}: {exc}')
            continue
        profile_errors = validate_workload_profile(profile, expected_id=profile_id)
        errors.extend(f'{path}: {error}' for error in profile_errors)
        files = profile.get('files', {}) if isinstance(profile, dict) else {}
        if isinstance(files, dict):
            for role, values in files.items():
                if not isinstance(values, list):
                    continue
                for value in values:
                    try:
                        relative = _safe_relative_path(value)
                    except ValueError:
                        continue
                    asset = root / relative
                    if not asset.is_file():
                        errors.append(f'{path}: missing files.{role} asset {value}')
    return errors


def _manifest_file_roles(profile: dict[str, Any] | None) -> dict[str, str]:
    roles: dict[str, str] = {}
    if profile is None:
        return roles
    singular = {
        'schema': 'schema',
        'generators': 'generator',
        'setup': 'setup',
        'queries': 'query',
    }
    for role, values in profile['files'].items():
        for value in values:
            roles[value] = singular.get(role, role)
    return roles


def _source_paths(root: Path, profile: dict[str, Any] | None) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() not in _SOURCE_SUFFIXES:
            raise ValueError(f'unsupported workload source file: {root}')
        return [root]
    if not root.is_dir():
        raise ValueError(f'workload path does not exist: {root}')
    if profile is not None:
        paths = [
            root / _safe_relative_path(value)
            for values in profile['files'].values()
            for value in values
        ]
        paths.append(root / 'profile.json')
        return sorted(paths)
    return sorted(
        path
        for path in root.rglob('*')
        if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES
    )


def _embedded_source(root: Path, path: Path, roles: dict[str, str]) -> dict[str, Any]:
    resolved_root = root.resolve() if root.is_dir() else root.resolve().parent
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f'workload source escapes root: {path}') from exc
    if path.is_symlink():
        raise ValueError(f'workload source must not be a symlink: {path}')
    size = resolved.stat().st_size
    if size > _MAX_SOURCE_BYTES:
        raise ValueError(f'workload source exceeds {_MAX_SOURCE_BYTES} bytes: {path}')
    try:
        content = resolved.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f'workload source is not UTF-8 text: {path}') from exc
    relative_name = relative.as_posix()
    suffix = resolved.suffix.lower()
    is_manifest = relative_name == 'profile.json'
    return {
        'path': relative_name,
        'role': 'manifest'
        if is_manifest
        else roles.get(relative_name, 'generator' if suffix == '.py' else 'query'),
        'media_type': (
            'application/json'
            if is_manifest
            else 'text/x-python'
            if suffix == '.py'
            else 'application/sql'
        ),
        'size_bytes': size,
        'hash': file_hash(resolved),
        'content': content,
    }


def build_workload_evidence(
    workload_conf: dict[str, Any], load_iterations: list[list[str]]
) -> dict[str, Any]:
    """Embed complete source and exact execution parameters into a report."""
    profile_id = workload_conf.get('workload_profile')
    profile = load_workload_profile(str(profile_id)) if profile_id else None
    raw_root = workload_conf.get('workload_path')
    if raw_root:
        root = Path(str(raw_root)).expanduser()
        sources = [
            _embedded_source(root, path, _manifest_file_roles(profile))
            for path in _source_paths(root, profile)
        ]
    else:
        sources = []
    if profile is None:
        init_template = str(workload_conf.get('init_command') or '')
        workload_template = str(workload_conf.get('workload_command') or '')
        for source in sources:
            if source['role'] == 'generator':
                continue
            if source['path'] in init_template:
                source['role'] = 'schema'
            elif source['path'] in workload_template:
                source['role'] = 'query'
            else:
                source['role'] = 'asset'
    source_fingerprints = [
        {key: source[key] for key in ('path', 'role', 'size_bytes', 'hash')} for source in sources
    ]
    definition = {
        'profile_id': profile_id or 'custom',
        'benchmark_type': str(workload_conf.get('benchmark_type')),
        'init_command_template': workload_conf.get('init_command'),
        'workload_command_template': workload_conf.get('workload_command'),
        'workload_scale': workload_conf.get('workload_scale'),
        'sources': source_fingerprints,
    }
    resolved_commands = [
        {'iteration_index': index, 'init': commands[0], 'workload': commands[1]}
        for index, commands in enumerate(load_iterations, start=1)
    ]
    pgbench = {
        'pgbench_path': workload_conf.get('pgbench_path'),
        'psql_path': workload_conf.get('psql_path'),
        'iteration_parameter': workload_conf.get('pgbench_iter_name'),
        'iteration_values': list(workload_conf.get('pgbench_iter_list') or []),
        'init_command_template': workload_conf.get('init_command'),
        'workload_command_template': workload_conf.get('workload_command'),
        'resolved_commands': resolved_commands,
    }
    definition_hash = canonical_hash(definition)
    execution_hash = canonical_hash(
        {
            'definition_hash': definition_hash,
            'pgbench_path': pgbench['pgbench_path'],
            'psql_path': pgbench['psql_path'],
            'iteration_parameter': pgbench['iteration_parameter'],
            'iteration_values': pgbench['iteration_values'],
            'workload_duration_seconds': workload_conf.get('workload_duration_seconds'),
        }
    )
    return {
        'schema_version': WORKLOAD_EVIDENCE_SCHEMA_VERSION,
        'profile_id': profile_id or 'custom',
        'source': f'bundled:{profile_id}' if profile_id else 'custom',
        'definition_hash': definition_hash,
        'execution_hash': execution_hash,
        'files': sources,
        'pgbench': pgbench,
    }
