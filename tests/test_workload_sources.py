import hashlib
import json
import shlex
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pg_perf_bench.report.commands import workload_parse
from pg_perf_bench.report.processing import save_report
from pg_perf_bench.workloads import build_workload_evidence


def custom_workload(root):
    return {
        'benchmark_type': 'custom',
        'workload_path': str(root),
        'init_command': 'python3 ARG_WORKLOAD_PATH/generator.py',
        'workload_command': 'pgbench -f ARG_WORKLOAD_PATH/load.sql',
        'workload_scale': 0.1,
        'workload_duration_seconds': 2,
        'pgbench_iter_name': 'pgbench_clients',
        'pgbench_iter_list': [1],
    }


@pytest.mark.parametrize('with_manifest', [False, True])
def test_custom_profile_keeps_configuration_and_changes_both_hashes(tmp_path, with_manifest):
    root = tmp_path / 'profile'
    root.mkdir()
    (root / 'generator.py').write_text('import json\n', encoding='utf-8')
    (root / 'load.sql').write_text('SELECT 1;\n', encoding='utf-8')
    config = root / 'dataset.json'
    config.write_text('{"scale_multiplier": 2}', encoding='utf-8')
    (root / 'settings.yaml').write_text('distribution: uniform\n', encoding='utf-8')
    (root / 'generator-settings').write_text('seed=42\n', encoding='utf-8')
    if with_manifest:
        (root / 'profile.json').write_text(
            json.dumps({'files': {'configuration': ['dataset.json']}}), encoding='utf-8'
        )
    for name in ('__pycache__', '.git', '.venv'):
        directory = root / name
        directory.mkdir()
        (directory / 'ignored').write_bytes(b'\xff\xfe')
    (root / 'generator.pyc').write_bytes(b'\xff\xfe')
    workload = custom_workload(root)
    commands = [[f'python3 {root}/generator.py', f'pgbench -f {root}/load.sql']]
    first = build_workload_evidence(workload, commands)
    files = {entry['path']: entry for entry in first['files']}
    expected = {'generator.py', 'load.sql', 'dataset.json', 'settings.yaml', 'generator-settings'}
    if with_manifest:
        expected.add('profile.json')
        assert files['profile.json']['role'] == 'manifest'
    assert set(files) == expected
    assert files['dataset.json']['content'] == config.read_text()
    assert files['dataset.json']['media_type'] == 'application/json'
    assert files['settings.yaml']['media_type'] == 'application/yaml'
    assert files['generator-settings']['media_type'] == 'text/plain'

    config.write_text('{"scale_multiplier": 7}', encoding='utf-8')
    second = build_workload_evidence(workload, commands)
    assert second['definition_hash'] != first['definition_hash']
    assert second['execution_hash'] != first['execution_hash']
    if with_manifest:
        manifest = root / 'profile.json'
        manifest.write_text(manifest.read_text() + '\n', encoding='utf-8')
        third = build_workload_evidence(workload, commands)
        assert third['definition_hash'] != second['definition_hash']
        assert third['execution_hash'] != second['execution_hash']


@pytest.mark.parametrize('file_option', ['-f {}', '--file={}', '--file {}', '-f{}'])
@pytest.mark.parametrize('relative', [False, True])
def test_bundled_override_embeds_external_sql_and_hashes_it(
    tmp_path, monkeypatch, file_option, relative
):
    from pg_perf_bench.const import WORKLOAD_PROFILES_PATH

    monkeypatch.chdir(tmp_path)
    script = tmp_path / 'outside script.sql'
    script.write_text('SELECT 314159;\n', encoding='utf-8')
    script_arg = script.name if relative else str(script)
    command = 'pgbench ' + file_option.format(shlex.quote(script_arg + '@25'))
    workload = custom_workload(WORKLOAD_PROFILES_PATH / 'pagila')
    workload.update(workload_profile='pagila', workload_command=command)
    commands = [['true', command], ['true', command]]
    first = build_workload_evidence(workload, commands)
    files = first['files']
    external = [entry for entry in files if entry.get('external')]
    assert len(external) == 1  # the two iterations share one snapshot
    assert external[0]['path'] == str(script)
    assert external[0]['role'] == 'query'
    assert external[0]['content'] == script.read_text()
    assert len(files) == 13  # the twelve bundled files are preserved too

    script.write_text('SELECT 271828;\n', encoding='utf-8')
    second = build_workload_evidence(workload, commands)
    assert second['definition_hash'] != first['definition_hash']
    assert second['execution_hash'] != first['execution_hash']


def test_external_init_files_and_internal_queries_keep_their_roles(tmp_path):
    root = tmp_path / 'profile'
    root.mkdir()
    query = root / 'load.sql'
    query.write_text('SELECT 1;\n', encoding='utf-8')
    schema = tmp_path / 'schema@2'
    schema.write_text('CREATE TABLE t (id integer);\n', encoding='utf-8')
    commands = [[f'psql --file={schema}', f'pgbench -f {query}@50 -f -@1']]
    evidence = build_workload_evidence(custom_workload(root), commands)
    files = {source['path']: source for source in evidence['files']}
    assert len(files) == 2
    assert files[str(schema)]['role'] == 'schema'
    assert files['load.sql']['role'] == 'query'
    assert 'external' not in files['load.sql']


def test_manifest_and_assets_are_in_visible_report_sections(tmp_path):
    root = tmp_path / 'profile'
    root.mkdir()
    (root / 'profile.json').write_text(
        '{"files":{"configuration":["dataset.json"]}}', encoding='utf-8'
    )
    (root / 'dataset.json').write_text('{"scale_multiplier":2}', encoding='utf-8')
    (root / 'load.sql').write_text('SELECT 42;\n', encoding='utf-8')
    evidence = build_workload_evidence(custom_workload(root), [['true', 'true']])
    init, query = {}, {}
    workload_parse({'workload_evidence': evidence}, init, phase='init')
    workload_parse({'workload_evidence': evidence}, query, phase='workload')
    assert '[manifest] profile.json' in init['data']
    assert '[configuration] dataset.json' in init['data']
    assert '[query] load.sql' in query['data']
    report = {
        'report_name': 'profile-sources',
        'workload_evidence': evidence,
        'sections': {'benchmark': {'reports': {'custom_tables': init, 'custom_workload': query}}},
    }
    paths = save_report(MagicMock(), report, tmp_path / 'reports')
    saved = json.loads(Path(paths['json']).read_text())
    assert saved['sections']['benchmark']['reports']['custom_tables']['data'] == init['data']
    assert '[manifest] profile.json' in Path(paths['html']).read_text()
    assert '[configuration] dataset.json' in Path(paths['html']).read_text()


@pytest.mark.parametrize('kind', ['missing', 'symlink', 'binary', 'oversize'])
def test_unarchivable_external_sql_fails_before_execution(tmp_path, monkeypatch, kind):
    script = tmp_path / 'outside.sql'
    if kind == 'symlink':
        target = tmp_path / 'target.sql'
        target.write_text('SELECT 1;\n', encoding='utf-8')
        script.symlink_to(target)
    elif kind == 'binary':
        script.write_bytes(b'\xff\xfe')
    elif kind == 'oversize':
        monkeypatch.setattr('pg_perf_bench.workloads._MAX_SOURCE_BYTES', 4)
        script.write_text('SELECT 1;\n', encoding='utf-8')
    with pytest.raises(ValueError):
        build_workload_evidence({}, [['true', f'pgbench -f {script}']])


def test_local_manifest_cannot_escape_profile_root(tmp_path):
    root = tmp_path / 'profile'
    root.mkdir()
    (root / 'profile.json').write_text(
        '{"files":{"configuration":["../outside.json"]}}', encoding='utf-8'
    )
    with pytest.raises(ValueError, match='unsafe workload asset path'):
        build_workload_evidence(custom_workload(root), [['true', 'true']])


def test_configured_client_name_and_source_bytes_are_preserved(tmp_path):
    script = tmp_path / 'load.sql'
    original = b'-- query with CRLF\r\nSELECT 1;\r\n'
    script.write_bytes(original)
    workload = {'pgbench_path': '/opt/bin/benchmark-client'}
    evidence = build_workload_evidence(
        workload, [['true', f'/opt/bin/benchmark-client -f {script}@7']]
    )
    source = evidence['files'][0]
    assert source['content'].encode('utf-8') == original
    assert source['size_bytes'] == len(original)
    assert source['hash'] == 'sha256:' + hashlib.sha256(original).hexdigest()
