import asyncio
import json
import shlex
import subprocess
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_perf_bench.benchmark import BenchmarkRunner
from pg_perf_bench.connections.local import LocalConnection
from pg_perf_bench.db_operations.patroni import (
    PYTHON_REQUIREMENT,
    PatroniController,
    _checked_script,
)
from pg_perf_bench.db_operations.patroni_member import member_action
from pg_perf_bench.db_operations.patroni_probe import discover, patroni_command, process_context
from pg_perf_bench.db_operations.patroni_probe import main as probe_main

START = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
MEMBER = {
    'name': 'node-1',
    'scope': 'bench',
    'data_directory': '/data',
    'postmaster_start_time': START.isoformat(),
    'process_id': 123,
}
WORKLOAD = {'pg_data_path': '/data', 'pg_bin_path': '/bin', 'allow_database_reset': True}


@pytest.mark.parametrize('version', [(3, 6), (3, 9), (3, 10), (3, 12)])
def test_remote_helper_checks_minimum_python_before_parsing_source(version):
    source = 'print("helper executed")' if version >= (3, 10) else 'not valid Python source!'
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            f'import sys; sys.version_info = {version!r}\n' + _checked_script(source),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not result.stderr
    if version < (3, 10):
        assert json.loads(result.stdout) == {'error': PYTHON_REQUIREMENT}
    else:
        assert result.stdout.strip() == 'helper executed'


@pytest.mark.parametrize('compatible_process', [False, True])
def test_discovery_skips_old_path_python_and_checks_running_interpreters(
    tmp_path, compatible_process
):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    old_python = bin_dir / 'python3'
    old_python.write_text('#!/bin/sh\nexit 1\n')
    old_python.chmod(0o755)
    # Simulate /proc pointing to either the same old executable or a supported venv.
    candidate = sys.executable if compatible_process else str(old_python)
    readlink = bin_dir / 'readlink'
    readlink.write_text('#!/bin/sh\nprintf "%s\\n" ' + shlex.quote(candidate) + '\n')
    readlink.chmod(0o755)
    conn = LocalConnection({'PATH': str(bin_dir)}, command_timeout=10)
    if compatible_process:
        assert asyncio.run(PatroniController.detect(conn, str(tmp_path))) is None
    else:
        with pytest.raises(RuntimeError, match='require Python 3.10 or newer'):
            asyncio.run(PatroniController.detect(conn, str(tmp_path)))


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        (
            ['/opt/venv/bin/python3', '/opt/venv/bin/patroni', '/etc/member.yaml'],
            ('/opt/venv/bin/python3', ['/etc/member.yaml']),
        ),
        (['python3', '-m', 'patroni'], ('python3', [])),
        (['python3', '-u', '-m', 'patroni'], ('python3', [])),
        (['/usr/bin/patroni', '/etc/patroni.d'], ('/usr/bin/patroni', ['/etc/patroni.d'])),
        (['patronictl', 'restart'], None),
        (['bash', '-c', 'patroni /etc/member.yaml'], None),
        (['postgres', '-D', '/patroni/data'], None),
        (['python3', 'unrelated.py', 'patroni'], None),
    ],
)
def test_process_recognition(argv, expected):
    assert patroni_command(argv) == expected


def _process(proc, pid, *argv):
    process = proc / str(pid)
    process.mkdir(parents=True)
    (process / 'cmdline').write_bytes(('\0'.join(argv) + '\0').encode())
    return process


def test_process_context_preserves_venv_environment_and_working_directory(tmp_path):
    process = tmp_path / 'proc'
    process.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    patroni = bindir / 'patroni'
    patroni.write_text('#!/opt/patroni-venv/bin/python3\n')
    patroni.chmod(0o755)
    (process / 'environ').write_text(f'PATH={bindir}\0PATRONI_NAME=member\0')
    (process / 'cwd').symlink_to(tmp_path)
    executable, env, cwd = process_context(process, 'patroni')
    assert executable == '/opt/patroni-venv/bin/python3'
    assert env['PATRONI_NAME'] == 'member'
    assert cwd == str(tmp_path)


@pytest.mark.parametrize('response', ['{}', '[]', '{"name":"node"}', 'invalid JSON'])
def test_invalid_probe_output_cannot_select_pg_ctl(response):
    conn = MagicMock(command_timeout=30, run_command=AsyncMock(return_value=response))
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(PatroniController.detect(conn, '/data'))


def test_root_probe_reads_processes_as_data_directory_owner(tmp_path):
    owner = SimpleNamespace(pw_name='postgres', pw_uid=123, pw_gid=456)
    with (
        patch.object(sys, 'argv', ['probe', 'detect', str(tmp_path), 'helper', '30']),
        patch('os.geteuid', return_value=0),
        patch('pwd.getpwuid', return_value=owner),
        patch('os.initgroups') as groups,
        patch('os.setgid') as group,
        patch('os.setuid') as user,
        patch('pg_perf_bench.db_operations.patroni_probe.discover', return_value=None),
    ):
        probe_main()
    groups.assert_called_once_with('postgres', 456)
    group.assert_called_once_with(456)
    user.assert_called_once_with(123)


def test_discovery_matches_only_selected_instance_and_ignores_installed_cli(tmp_path):
    proc = tmp_path / 'proc'
    _process(proc, 1, 'python3', '/usr/bin/patroni', '/etc/other.yaml')
    _process(proc, 2, 'python3', '/usr/bin/patroni', '/etc/target.yaml')
    _process(proc, 3, 'patronictl', 'list')
    with patch(
        'pg_perf_bench.db_operations.patroni_probe.invoke_member',
        side_effect=lambda process, *args: deepcopy(MEMBER) if process.name == '2' else None,
    ) as invoke:
        result = discover('/data', 'helper', 10, proc)
    assert result['process_id'] == 2
    assert invoke.call_count == 2


@pytest.mark.parametrize('marker', ['patroni.dynamic.json', 'postgresql.conf'])
def test_patroni_markers_without_accessible_process_never_fall_back(tmp_path, marker):
    proc = tmp_path / 'proc'
    proc.mkdir()
    (tmp_path / marker).write_text('# It will be overwritten by Patroni!')
    with pytest.raises(RuntimeError, match='running process could not be identified'):
        discover(str(tmp_path), 'helper', 10, proc)


def test_discovery_fails_on_ambiguous_or_unreadable_processes(tmp_path):
    proc = tmp_path / 'proc'
    _process(proc, 1, 'patroni', '/etc/a.yaml')
    _process(proc, 2, 'patroni', '/etc/b.yaml')
    with (
        patch(
            'pg_perf_bench.db_operations.patroni_probe.invoke_member', return_value=deepcopy(MEMBER)
        ),
        pytest.raises(RuntimeError, match='Multiple Patroni'),
    ):
        discover('/data', 'helper', 10, proc)
    with (
        patch(
            'pg_perf_bench.db_operations.patroni_probe.invoke_member',
            side_effect=PermissionError('Cannot read Patroni configuration'),
        ),
        pytest.raises(RuntimeError, match='Cannot read'),
    ):
        discover('/data', 'helper', 10, proc)


def _member_api(tmp_path, *, response_code=200, role='primary', name='node-1'):
    config = {
        'name': 'node-1',
        'scope': 'bench',
        'postgresql': {'data_dir': str(tmp_path)},
        'restapi': {
            'listen': '0.0.0.0:18081',
            'connect_address': 'db.example:18081',
            'certfile': '/tls/server.crt',
            'auth': 'api-user:private-password',
        },
        'ctl': {'cacert': '/tls/ca.crt', 'certfile': '/tls/client.crt'},
    }
    status = {
        'patroni': {'name': name, 'scope': 'bench'},
        'role': role,
        'state': 'running',
        'postmaster_start_time': START.isoformat(),
    }
    http = MagicMock()
    http.request.side_effect = [
        SimpleNamespace(status=200, data=json.dumps(status).encode()),
        SimpleNamespace(status=response_code, data=b'private-response-body'),
    ]
    factory = MagicMock(return_value=http)
    modules = {
        'patroni': SimpleNamespace(),
        'patroni.config': SimpleNamespace(Config=MagicMock(return_value=config)),
        'patroni.request': SimpleNamespace(PatroniRequest=factory),
    }
    return config, modules, http, factory


def test_member_restart_uses_configured_api_tls_auth_and_does_not_retry(tmp_path):
    config, modules, http, factory = _member_api(tmp_path)
    alias = tmp_path / 'data-link'
    alias.symlink_to(tmp_path, target_is_directory=True)
    with patch.dict(sys.modules, modules):
        result = member_action('restart', str(alias), '/etc/patroni.d', 20)
    assert result['name'] == 'node-1'
    factory.assert_called_once_with(config)
    http.request.assert_called_with(
        'POST',
        'https://db.example:18081/restart',
        {'role': 'primary'},
        timeout=20,
        retries=False,
        redirect=False,
    )
    assert 'private-password' not in json.dumps(result)


@pytest.mark.parametrize('status', [202, 401, 403, 500, 503])
def test_failed_or_scheduled_restart_is_not_reported_as_success(tmp_path, status):
    _, modules, http, _ = _member_api(tmp_path, response_code=status)
    with patch.dict(sys.modules, modules), pytest.raises(RuntimeError, match=f'HTTP {status}'):
        member_action('restart', str(tmp_path), '/etc/patroni.yaml', 20)
    assert http.request.call_count == 2


@pytest.mark.parametrize(('role', 'name'), [('replica', 'node-1'), ('primary', 'other')])
def test_member_identity_and_role_are_checked_before_restart(tmp_path, role, name):
    _, modules, http, _ = _member_api(tmp_path, role=role, name=name)
    with patch.dict(sys.modules, modules), pytest.raises(RuntimeError):
        member_action('restart', str(tmp_path), '/etc/patroni.yaml', 20)
    assert http.request.call_count == 1


def test_other_data_directory_does_not_contact_api(tmp_path):
    _, modules, http, factory = _member_api(tmp_path)
    with patch.dict(sys.modules, modules):
        assert member_action('inspect', str(tmp_path / 'other'), '', 20) is None
    factory.assert_not_called()
    http.request.assert_not_called()


@pytest.mark.parametrize(
    ('listen', 'expected'),
    [
        ('0.0.0.0:19001', '127.0.0.1:19001'),
        ('[::]:19002', '[::1]:19002'),
        ('::1:19003', '[::1]:19003'),
    ],
)
def test_api_listen_address_fallback_supports_wildcards_and_ipv6(tmp_path, listen, expected):
    config, modules, http, _ = _member_api(tmp_path)
    config['restapi'] = {'listen': listen}
    with patch.dict(sys.modules, modules):
        member_action('inspect', str(tmp_path), '/etc/patroni.yaml', 20)
    http.request.assert_called_once_with(
        'GET',
        f'http://{expected}/patroni',
        None,
        timeout=20,
        retries=False,
        redirect=False,
    )


def _database(start_time=START):
    state = {'start': start_time}
    sql = MagicMock()

    async def fetchval(query):
        return state['start'] if 'pg_postmaster_start_time' in query else False

    sql.fetchval = AsyncMock(side_effect=fetchval)

    @asynccontextmanager
    async def connect(_database):
        yield sql

    tasks = MagicMock()
    tasks._connect = connect
    for method in ('check_db_access', 'drop_db', 'init_db', 'check_user_db_access'):
        setattr(tasks, method, AsyncMock())
    return tasks, state


@pytest.mark.parametrize('transport', ['local', 'ssh', 'docker'])
def test_reset_restarts_patroni_without_pg_ctl_or_container_stop(transport):
    db, state = _database()
    lifecycle = MagicMock(sync=AsyncMock(), start_db=AsyncMock(), stop_db=AsyncMock())

    async def command(_conn, _path, action, _pid=None):
        if action == 'restart':
            state['start'] += timedelta(seconds=1)
        return {**MEMBER, 'postmaster_start_time': state['start'].isoformat()}

    with (
        patch.object(PatroniController, '_command', AsyncMock(side_effect=command)) as api,
        patch('pg_perf_bench.benchmark.DBTasks', return_value=db),
        patch('pg_perf_bench.benchmark.get_conn_type_tasks', return_value=lambda **kw: lifecycle),
    ):
        asyncio.run(
            BenchmarkRunner.reset_db_environment(
                MagicMock(),
                transport,
                MagicMock(),
                {},
                WORKLOAD,
            )
        )
    assert [call.args[2] for call in api.await_args_list] == ['detect', 'restart', 'detect']
    lifecycle.start_db.assert_not_awaited()
    lifecycle.stop_db.assert_not_awaited()
    lifecycle.sync.assert_awaited_once()
    db.drop_db.assert_awaited_once()
    db.init_db.assert_awaited_once()
    db.check_user_db_access.assert_awaited_once()


@pytest.mark.parametrize('option', ['drop_os_caches', 'pg_custom_config'])
def test_unsupported_options_fail_before_database_mutation(option):
    db, _ = _database()
    with (
        patch.object(PatroniController, '_command', AsyncMock(return_value=MEMBER)),
        patch('pg_perf_bench.benchmark.DBTasks', return_value=db),
        pytest.raises(RuntimeError, match=option.replace('_', '-')),
    ):
        asyncio.run(
            BenchmarkRunner.reset_db_environment(
                MagicMock(),
                'docker',
                MagicMock(),
                {},
                {**WORKLOAD, option: True},
            )
        )
    db.drop_db.assert_not_awaited()
    db.init_db.assert_not_awaited()


def test_sql_target_mismatch_aborts_before_dropping_database():
    db, _ = _database(START + timedelta(seconds=10))
    with (
        patch.object(PatroniController, '_command', AsyncMock(return_value=MEMBER)) as api,
        patch('pg_perf_bench.benchmark.DBTasks', return_value=db),
        pytest.raises(RuntimeError, match='SQL connection does not reach'),
    ):
        asyncio.run(
            BenchmarkRunner.reset_db_environment(
                MagicMock(),
                'docker',
                MagicMock(),
                {},
                WORKLOAD,
            )
        )
    assert api.await_count == 1
    db.drop_db.assert_not_awaited()


@pytest.mark.parametrize('failure', ['api', 'unchanged_start'])
def test_restart_failure_never_falls_back_or_initializes_database(failure):
    db, _ = _database()
    lifecycle = MagicMock(sync=AsyncMock(), start_db=AsyncMock(), stop_db=AsyncMock())
    replies = [MEMBER, RuntimeError('Patroni HTTP 503')] if failure == 'api' else [MEMBER] * 3
    with (
        patch.object(PatroniController, '_command', AsyncMock(side_effect=replies)),
        patch('pg_perf_bench.benchmark.DBTasks', return_value=db),
        patch('pg_perf_bench.benchmark.get_conn_type_tasks', return_value=lambda **kw: lifecycle),
        pytest.raises(RuntimeError),
    ):
        asyncio.run(
            BenchmarkRunner.reset_db_environment(
                MagicMock(),
                'docker',
                MagicMock(),
                {},
                WORKLOAD,
            )
        )
    lifecycle.start_db.assert_not_awaited()
    lifecycle.stop_db.assert_not_awaited()
    db.init_db.assert_not_awaited()


def test_plain_postgres_preserves_stop_sync_start_order():
    events = []
    db, _ = _database()
    lifecycle = MagicMock()

    for method in ('start_db', 'stop_db', 'sync', 'drop_caches'):

        async def record(method=method):
            events.append(method)

        setattr(lifecycle, method, AsyncMock(side_effect=record))

    with (
        patch.object(PatroniController, 'detect', AsyncMock(return_value=None)),
        patch('pg_perf_bench.benchmark.DBTasks', return_value=db),
        patch('pg_perf_bench.benchmark.get_conn_type_tasks', return_value=lambda **kw: lifecycle),
    ):
        asyncio.run(
            BenchmarkRunner.reset_db_environment(
                MagicMock(),
                'docker',
                MagicMock(),
                {},
                {**WORKLOAD, 'drop_os_caches': True},
            )
        )
    assert events == ['start_db', 'stop_db', 'sync', 'drop_caches', 'start_db']
    db.init_db.assert_awaited_once()
