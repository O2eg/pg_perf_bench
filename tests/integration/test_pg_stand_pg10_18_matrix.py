"""Destructive opt-in compatibility matrix against disposable single-node pg_stand."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_PG_STAND_MATRIX') != '1',
        reason='set PG_PERF_BENCH_PG_STAND_MATRIX=1 to run PostgreSQL 10-18',
    ),
]


def _run(arguments, *, cwd, env=None, accepted_codes=(0,), timeout=300):
    result = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode not in accepted_codes:
        raise AssertionError(
            f'command failed ({result.returncode}): {arguments}\n'
            f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        )
    return result


@pytest.mark.parametrize('server_major', range(10, 19))
def test_newest_local_pgbench_against_pg_stand_single_node(tmp_path, server_major):
    pg_stand = Path(
        os.environ.get(
            'PG_STAND_BIN',
            '/home/oleg/Desktop/dev/pg_stand/.venv/bin/pg-stand',
        )
    )
    if not pg_stand.is_file():
        pytest.skip(f'pg-stand executable not found: {pg_stand}')

    stand_root = tmp_path / f'stand-pg{server_major}'
    _run([pg_stand, 'init', '--directory', stand_root], cwd=tmp_path)
    config = stand_root / 'configs' / 'single.yaml'
    common_stand_args = [pg_stand, '-c', config, '--pg-version', str(server_major)]
    stand_up = False
    try:
        _run([*common_stand_args, 'up', '--timeout', '240'], cwd=stand_root)
        stand_up = True
        credentials_path = stand_root / '.pg_stand' / 'credentials' / 'database' / 'passwords.json'
        password = json.loads(credentials_path.read_text(encoding='utf-8'))['superuser']['password']
        environment = os.environ.copy()
        environment['PGPASSWORD'] = password
        report_dir = tmp_path / 'report'
        container = 'pg-stand-single-primary-pg-stand-managed'
        pgdata = (
            f'/var/lib/postgresql/{server_major}/docker'
            if server_major >= 18
            else '/var/lib/postgresql/data'
        )
        _run(
            [
                sys.executable,
                '-m',
                'pg_perf_bench',
                'benchmark',
                '--connection-type',
                'docker',
                '--container-name',
                container,
                '--allow-database-reset',
                '--host',
                '127.0.0.1',
                '--port',
                '55432',
                '--user',
                'postgres',
                '--database',
                f'pg_perf_bench_pg{server_major}',
                '--pg-data-path',
                pgdata,
                '--pg-bin-path',
                f'/usr/lib/postgresql/{server_major}/bin',
                '--benchmark-type',
                'default',
                '--pgbench-clients',
                '1',
                '--init-command',
                ('ARG_PGBENCH_PATH -i -s 1 -h 127.0.0.1 -p 55432 -U postgres ARG_PG_DATABASE'),
                '--workload-command',
                (
                    'ARG_PGBENCH_PATH -T 1 -c ARG_PGBENCH_CLIENTS -j 1 '
                    '-h 127.0.0.1 -p 55432 -U postgres ARG_PG_DATABASE'
                ),
                '--command-timeout',
                '60',
                '--out',
                report_dir,
                '--log-dir',
                tmp_path / 'log',
                '--report-name',
                f'pg{server_major}-local-pgbench',
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            accepted_codes=(0, 5),
        )
        report_path = report_dir / f'pg{server_major}-local-pgbench.json'
        report_text = report_path.read_text(encoding='utf-8')
        report = json.loads(report_text)
        compatibility = report['postgresql_compatibility']
        assert compatibility['server']['major'] == server_major
        assert compatibility['load_generator']['execution_host'] == 'pg_perf_bench_local_host'
        assert compatibility['load_generator']['pgbench']['major'] >= 18
        assert report['benchmark_runs'][0]['metrics']['tps'] > 0
        assert report['sections']['os_metrics']['reports']['os_cpu_utilization']['data']
        assert password not in report_text
    finally:
        if stand_up:
            _run([*common_stand_args, 'down', '--clear-data'], cwd=stand_root)
