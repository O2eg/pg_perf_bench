"""Opt-in real Patroni/etcd lifecycle tests on a disposable pg_stand container."""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import docker
import pytest
import yaml

from pg_perf_bench.connections.docker import DockerConnection
from pg_perf_bench.db_operations.patroni import (
    PYTHON_REQUIREMENT,
    PatroniController,
    _checked_script,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_PATRONI_INTEGRATION') != '1',
        reason='set PG_PERF_BENCH_PATRONI_INTEGRATION=1 to provision Patroni and etcd',
    ),
]

DATA = '/var/lib/postgresql/18/docker'
BIN = '/usr/lib/postgresql/18/bin'
REPO = Path(__file__).parents[2]


@pytest.mark.parametrize('version', ['3.9', '3.10'])
def test_remote_python_minimum(version):
    client = docker.from_env()
    container = None
    try:
        image = f'python:{version}-slim'
        try:
            client.images.get(image)
        except docker.errors.ImageNotFound:
            pytest.skip(f'Integration image is not installed: {image}')
        container = client.containers.run(
            image,
            ['sleep', '60'],
            detach=True,
            name='pg-perf-python-' + uuid.uuid4().hex[:8],
        )

        class Connection:
            command_timeout = 10

            async def run_command(self, command, **kwargs):
                result = container.exec_run(['sh', '-c', command])
                assert result.exit_code == 0, result.output.decode()
                return result.output.decode()

        if version == '3.9':
            with pytest.raises(RuntimeError, match='require Python 3.10 or newer'):
                asyncio.run(PatroniController.detect(Connection(), '/tmp'))
            # The active Patroni interpreter is checked independently of discovery.
            member = (REPO / 'src/pg_perf_bench/db_operations/patroni_member.py').read_text()
            result = container.exec_run(['python3', '-c', _checked_script(member)])
            assert result.exit_code == 0
            assert json.loads(result.output) == {'error': PYTHON_REQUIREMENT}
        else:
            assert asyncio.run(PatroniController.detect(Connection(), '/tmp')) is None
    finally:
        if container is not None:
            container.remove(force=True, v=True)
        client.close()


def _run(args, *, cwd=REPO, env=None, accepted=(0,), timeout=240):
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode in accepted, result.stdout + result.stderr
    return result


def _exec(container, args, *, user='postgres'):
    result = container.exec_run(args, user=user)
    assert result.exit_code == 0, result.output.decode(errors='replace')
    return result.output.decode().strip()


def _copy(container, path, content, *, mode=0o644):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        info = tarfile.TarInfo(Path(path).name)
        info.size = len(content)
        info.mode = mode
        tar.addfile(info, io.BytesIO(content))
    assert container.put_archive(str(Path(path).parent), archive.getvalue())


def _sql(container, sql):
    return _exec(container, ['psql', '-XAt', '-U', 'postgres', '-d', 'postgres', '-c', sql])


def _api_tls(root, container):
    # Python 3.13's TLS server uses strict X.509 checks; generate an explicit
    # signing key usage rather than relying on the stand's PostgreSQL CA fixture.
    tls = root / 'patroni-tls'
    tls.mkdir(exist_ok=True)
    _run(
        [
            'openssl',
            'req',
            '-x509',
            '-newkey',
            'rsa:2048',
            '-nodes',
            '-days',
            '2',
            '-subj',
            '/CN=Patroni test CA',
            '-keyout',
            'ca.key',
            '-out',
            'ca.crt',
            '-addext',
            'basicConstraints=critical,CA:TRUE',
            '-addext',
            'keyUsage=critical,keyCertSign,cRLSign',
        ],
        cwd=tls,
    )
    _run(
        [
            'openssl',
            'req',
            '-new',
            '-newkey',
            'rsa:2048',
            '-nodes',
            '-subj',
            '/CN=localhost',
            '-keyout',
            'member.key',
            '-out',
            'member.csr',
        ],
        cwd=tls,
    )
    (tls / 'extensions.cnf').write_text(
        'basicConstraints=critical,CA:FALSE\n'
        'keyUsage=critical,digitalSignature,keyEncipherment\n'
        'extendedKeyUsage=serverAuth,clientAuth\n'
        'subjectAltName=DNS:localhost,IP:127.0.0.1\n'
        'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n'
    )
    _run(
        [
            'openssl',
            'x509',
            '-req',
            '-in',
            'member.csr',
            '-CA',
            'ca.crt',
            '-CAkey',
            'ca.key',
            '-CAcreateserial',
            '-days',
            '2',
            '-out',
            'member.crt',
            '-extfile',
            'extensions.cnf',
        ],
        cwd=tls,
    )
    for target, source in {
        'ca.crt': 'ca.crt',
        'server.crt': 'member.crt',
        'server.key': 'member.key',
        'postgres.crt': 'member.crt',
        'postgres.key': 'member.key',
    }.items():
        path = '/tmp/api-' + target
        _copy(container, path, (tls / source).read_bytes())
        _exec(container, ['chown', 'postgres:postgres', path], user='root')
        if target.endswith('.key'):
            _exec(container, ['chmod', '600', path], user='root')


def _free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def _detect(container):
    async def detect():
        async with DockerConnection(
            {'container_name': container.name},
            {},
            command_timeout=20,
        ) as conn:
            controller = await PatroniController.detect(conn, DATA)
            return controller.member if controller else None

    return asyncio.run(detect())


def _wait_for_patroni(container):
    deadline = time.monotonic() + 60
    last_error = 'No matching Patroni process'
    while time.monotonic() < deadline:
        try:
            member = _detect(container)
            if member:
                return member
        except RuntimeError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    pytest.fail('Patroni did not become a running primary: ' + last_error)


@pytest.fixture
def stand(tmp_path):
    stand_bin = Path(
        os.environ.get(
            'PG_STAND_BIN',
            '/home/oleg/Desktop/dev/pg_stand/.venv/bin/pg-stand',
        )
    )
    if not stand_bin.is_file():
        pytest.skip(f'pg-stand executable not found: {stand_bin}')
    root = tmp_path / 'stand'
    _run([stand_bin, 'init', '--directory', root])
    config_path = root / 'configs/single.yaml'
    config = yaml.safe_load(config_path.read_text())
    name = 'pg-perf-patroni-' + uuid.uuid4().hex[:8]
    config['metadata']['name'] = name
    config['spec']['storage']['root_directory'] = '.pg_stand/' + name
    config['spec']['docker']['network_name'] = name + '-network-pg-stand-managed'
    node = config['spec']['nodes']['primary']
    node['container_name'] = name + '-primary-pg-stand-managed'
    ports = _free_ports(5)
    for key, port in zip(
        [
            'published_port',
            'ssh_published_port',
            'pgbouncer_session_published_port',
            'pgbouncer_transaction_published_port',
        ],
        ports[:4],
        strict=True,
    ):
        node[key] = port
    config_path.write_text(yaml.safe_dump(config))
    # Isolate the stand's active lock from developer stands. Keep a supervisor
    # alive when Patroni restarts PG; the stock image normally exits with PG.
    runner = root / 'stand_cli.py'
    runner.write_text(
        """import time
import pg_stand.runtime as runtime
import pg_stand.runtime_common as common
from pg_stand.cli import main
runtime.ACTIVE_LOCK_NETWORK = common.ACTIVE_LOCK_NETWORK = """
        + repr(name + '-lock-pg-stand-managed')
        + """
original = runtime.primary_postgres_command
runtime.primary_postgres_command = lambda config: [
    'bash', '-c', 'docker-entrypoint.sh "$@" & wait "$!"; exec sleep infinity',
    'test-supervisor', *original(config)
]
def wait_for_postgres(self, container, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = container.exec_run(['pg_isready', '-h', '127.0.0.1', '-U', 'postgres'])
        if result.exit_code == 0:
            return
        time.sleep(0.2)
    raise RuntimeError('PostgreSQL readiness timeout')
runtime.StandManager._wait_for_postgres = wait_for_postgres
raise SystemExit(main())
"""
    )
    stand_args = [stand_bin.parent / 'python', runner, '-c', config_path]
    client = docker.from_env()
    try:
        _run([*stand_args, 'up', '--timeout', '180'], cwd=root)
        container = client.containers.get(node['container_name'])
        password = json.loads((root / '.pg_stand/credentials/database/passwords.json').read_text())[
            'superuser'
        ]['password']
        public_key = _exec(container, ['cat', '/etc/ssh/ssh_host_ed25519_key.pub'], user='root')
        (root / '.pg_stand/known_hosts').write_text(
            f'[127.0.0.1]:{node["ssh_published_port"]} {public_key}\n'
        )
        yield root, container, node, ports[4], password
    finally:
        _run([*stand_args, 'down', '--clear-data'], cwd=root)
        client.close()


def _arguments(root, node, *, transport, port, report_name, fast=False):
    args = [
        'benchmark',
        '--connection-type',
        transport,
        '--allow-database-reset',
        '--host',
        '127.0.0.1',
        '--port',
        str(port),
        '--user',
        'postgres',
        '--database',
        'pg_perf_patroni_test',
        '--pg-data-path',
        DATA,
        '--pg-bin-path',
        BIN,
        '--benchmark-type',
        'default',
        '--pgbench-clients',
        '1,2',
        '--init-command',
        'ARG_PGBENCH_PATH -i -s 1 -h ARG_PG_HOST -p ARG_PG_PORT -U postgres ARG_PG_DATABASE',
        '--workload-command',
        'ARG_PGBENCH_PATH -T 1 -c ARG_PGBENCH_CLIENTS -j 1 '
        '-h ARG_PG_HOST -p ARG_PG_PORT -U postgres ARG_PG_DATABASE',
        '--command-timeout',
        '60',
        '--out',
        str(root / 'reports'),
        '--log-dir',
        str(root / 'logs'),
        '--report-name',
        report_name,
    ]
    if fast:
        for option in ('--benchmark-type', '--init-command', '--workload-command'):
            index = args.index(option)
            del args[index : index + 2]
        args += [
            '--workload-profile',
            'pagila-htap',
            '--workload-scale',
            '0.01',
            '--workload-duration-seconds',
            '1',
            '--init-batch-rows',
            '137',
        ]
    if transport == 'docker':
        args += ['--container-name', node['container_name']]
    elif transport == 'ssh':
        args += [
            '--ssh-host',
            '127.0.0.1',
            '--ssh-port',
            str(node['ssh_published_port']),
            '--ssh-user',
            'root',
            '--ssh-key',
            str(root / '.pg_stand/credentials/ssh/pg_stand_test'),
            '--ssh-known-hosts',
            str(root / '.pg_stand/known_hosts'),
            '--remote-pg-host',
            '127.0.0.1',
            '--remote-pg-port',
            '5432',
        ]
    return args


def _check_report(text, *secrets):
    report = json.loads(text)
    assert len(report['benchmark_runs']) == 2
    assert all(run['metrics']['tps'] > 0 for run in report['benchmark_runs'])
    storage_items = report['sections']['storage']['reports']
    assert len(storage_items) == 12
    assert all(item['collection_status'] == 'ok' for item in storage_items.values())
    assert all(secret not in text for secret in secrets)
    assert report['benchmark_methodology']['server_restarted_before_each_iteration'] is True


def test_patroni_benchmark_all_transports_and_plain_postgres(stand):
    root, container, node, tunnel_port, password = stand
    env = {**os.environ, 'PGPASSWORD': password}
    # The unmodified pg_stand image has no Patroni: exercise the original path.
    assert _detect(container) is None
    _run(
        [
            sys.executable,
            '-m',
            'pg_perf_bench',
            *_arguments(
                root,
                node,
                transport='docker',
                port=node['published_port'],
                report_name='plain',
            ),
        ],
        env=env,
        accepted=(0, 5),
    )
    _check_report((root / 'reports/plain.json').read_text(), password)

    _exec(
        container,
        [
            'bash',
            '-lc',
            'apt-get update >/tmp/patroni-install.log 2>&1 && '
            'DEBIAN_FRONTEND=noninteractive apt-get install -y patroni etcd-server '
            'python3-etcd python3-venv >>/tmp/patroni-install.log 2>&1',
        ],
        user='root',
    )
    api_password = uuid.uuid4().hex
    patroni_config = {
        'scope': node['container_name'],
        'name': 'primary',
        'restapi': {
            'listen': '127.0.0.1:18081',
            'connect_address': '127.0.0.1:18081',
            'authentication': {'username': 'bench-api', 'password': api_password},
        },
        'etcd3': {'host': '127.0.0.1:2379'},
        'bootstrap': {'dcs': {'ttl': 20, 'loop_wait': 2, 'retry_timeout': 3}},
        'postgresql': {
            'data_dir': DATA,
            'bin_dir': BIN,
            'listen': '0.0.0.0:5432',
            'connect_address': '127.0.0.1:5432',
            'authentication': {
                'superuser': {'username': 'postgres', 'password': password},
                'replication': {'username': 'postgres', 'password': password},
            },
        },
    }
    patroni_yaml = yaml.safe_dump(patroni_config)
    _copy(container, '/tmp/patroni.yaml', patroni_yaml.encode(), mode=0o600)
    _exec(container, ['chown', 'postgres:postgres', '/tmp/patroni.yaml'], user='root')
    container.exec_run(
        [
            'bash',
            '-lc',
            'exec etcd --data-dir=/tmp/bench-etcd >/tmp/etcd.log 2>&1',
        ],
        user='postgres',
        detach=True,
    )
    container.exec_run(
        [
            'bash',
            '-lc',
            'exec patroni /tmp/patroni.yaml >/tmp/patroni.log 2>&1',
        ],
        user='postgres',
        detach=True,
    )
    member = _wait_for_patroni(container)

    # Reproduce the old race: Patroni brings PG back while the benchmark assumes
    # it is stopped, so a subsequent pg_ctl start fails.
    _exec(container, [BIN + '/pg_ctl', 'stop', '-D', DATA, '-w'])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready = container.exec_run(['pg_isready', '-h', '127.0.0.1'], user='postgres')
        if ready.exit_code == 0:
            break
        time.sleep(0.5)
    else:
        pytest.fail('Patroni did not restart PostgreSQL after pg_ctl stop')
    assert (
        container.exec_run(
            [BIN + '/pg_ctl', 'start', '-D', DATA, '-w'],
            user='postgres',
        ).exit_code
        != 0
    )
    _wait_for_patroni(container)

    # Incompatible options must fail before a database drop or config upload.
    custom = root / 'custom.conf'
    custom.write_text('shared_buffers = 128MB\n')
    before_oid = _sql(container, "select oid from pg_database where datname='pg_perf_patroni_test'")
    before_config = _exec(container, ['sha256sum', DATA + '/postgresql.conf'])
    for option in [['--drop-os-caches'], ['--pg-custom-config', str(custom)]]:
        rejected = _run(
            [
                sys.executable,
                '-m',
                'pg_perf_bench',
                *_arguments(
                    root,
                    node,
                    transport='docker',
                    port=node['published_port'],
                    report_name='rejected',
                ),
                *option,
            ],
            env=env,
            accepted=(6,),
        )
        assert option[0] in rejected.stderr
        assert before_oid == _sql(
            container,
            "select oid from pg_database where datname='pg_perf_patroni_test'",
        )
        assert before_config == _exec(container, ['sha256sum', DATA + '/postgresql.conf'])

    container.reload()
    container_start = container.attrs['State']['StartedAt']
    for transport, port in [('docker', node['published_port']), ('ssh', tunnel_port)]:
        previous = _sql(container, 'select pg_postmaster_start_time()')
        name = 'patroni-' + transport
        _run(
            [
                sys.executable,
                '-m',
                'pg_perf_bench',
                *_arguments(
                    root,
                    node,
                    transport=transport,
                    port=port,
                    report_name=name,
                    fast=True,
                ),
            ],
            env=env,
            accepted=(0, 5),
        )
        _check_report((root / f'reports/{name}.json').read_text(), password, api_password)
        fast_report = json.loads((root / f'reports/{name}.json').read_text())
        assert fast_report['workload_evidence']['initialization']['mode'] == 'fast'
        assert all(
            run['initialization']['fsync_after'] == 'on' for run in fast_report['benchmark_runs']
        )
        assert _sql(container, 'SHOW fsync') == 'on'
        assert (
            _sql(
                container,
                "SELECT count(*) FROM pg_file_settings WHERE name='fsync' "
                "AND sourcefile LIKE '%postgresql.auto.conf'",
            )
            == '0'
        )
        assert (root / f'reports/{name}.html').is_file()
        assert previous != _sql(container, 'select pg_postmaster_start_time()')
        container.reload()
        assert container.attrs['State']['StartedAt'] == container_start
        assert _detect(container)['process_id'] == member['process_id']

    # Cover environment-only configuration and a venv entry point outside PATH.
    _exec(container, ['kill', '-TERM', str(member['process_id'])])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if container.exec_run(['test', '-d', f'/proc/{member["process_id"]}']).exit_code:
            break
        time.sleep(0.5)
    _exec(
        container,
        ['python3', '-m', 'venv', '--system-site-packages', '/tmp/patroni-venv'],
        user='root',
    )
    _api_tls(root, container)
    patroni_config['restapi'].update(
        {
            'certfile': '/tmp/api-server.crt',
            'keyfile': '/tmp/api-server.key',
            'cafile': '/tmp/api-ca.crt',
            'verify_client': 'required',
        }
    )
    patroni_config['ctl'] = {
        'cacert': '/tmp/api-ca.crt',
        'certfile': '/tmp/api-postgres.crt',
        'keyfile': '/tmp/api-postgres.key',
    }
    patroni_yaml = yaml.safe_dump(patroni_config)
    container.exec_run(
        [
            'bash',
            '-lc',
            'exec /tmp/patroni-venv/bin/python -m patroni >/tmp/patroni-env.log 2>&1',
        ],
        user='postgres',
        detach=True,
        environment={'PATRONI_CONFIGURATION': patroni_yaml},
    )
    _wait_for_patroni(container)

    # Run the local transport inside the database host, from the built wheel.
    dist = root / 'dist'
    _run([sys.executable, '-m', 'build', '--wheel', '--outdir', dist])
    wheel = next(dist.glob('*.whl'))
    _copy(container, '/tmp/' + wheel.name, wheel.read_bytes())
    _exec(container, ['python3', '-m', 'venv', '/tmp/bench-venv'], user='root')
    _exec(
        container,
        [
            '/tmp/bench-venv/bin/pip',
            'install',
            '/tmp/' + wheel.name,
        ],
        user='root',
    )
    previous = _sql(container, 'select pg_postmaster_start_time()')
    result = container.exec_run(
        [
            '/tmp/bench-venv/bin/pg-perf-bench',
            *_arguments(
                Path('/tmp/local-bench'),
                node,
                transport='local',
                port=5432,
                report_name='local',
            ),
        ],
        user='root',
        environment={'PGPASSWORD': password},
    )
    assert result.exit_code in {0, 5}, result.output.decode(errors='replace')
    report = _exec(container, ['cat', '/tmp/local-bench/reports/local.json'], user='root')
    _check_report(report, password, api_password)
    assert previous != _sql(container, 'select pg_postmaster_start_time()')
    container.reload()
    assert container.attrs['State']['StartedAt'] == container_start
