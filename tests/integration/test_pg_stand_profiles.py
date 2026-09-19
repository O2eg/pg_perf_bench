"""Opt-in end-to-end run of every bundled workload profile on a disposable pg_stand."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pg_perf_bench.workloads import bundled_profile_names

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_PG_STAND_PROFILES') != '1',
        reason='set PG_PERF_BENCH_PG_STAND_PROFILES=1 to run bundled profiles on pg_stand',
    ),
]


def _run(arguments, *, cwd, env=None, accepted_codes=(0,), timeout=600):
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


@pytest.mark.parametrize('profile_id', bundled_profile_names())
def test_bundled_profile_runs_on_pg_stand_single_node(tmp_path, profile_id):
    pg_stand = Path(
        os.environ.get(
            'PG_STAND_BIN',
            str(Path(sys.executable).with_name('pg-stand')),
        )
    )
    if not pg_stand.is_file():
        pytest.skip(f'pg-stand executable not found: {pg_stand}')
    server_major = int(os.environ.get('PG_PERF_BENCH_PG_STAND_VERSION', '18'))

    stand_root = tmp_path / f'stand-{profile_id}'
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
                'pg-stand-single-primary-pg-stand-managed',
                '--allow-database-reset',
                '--host',
                '127.0.0.1',
                '--port',
                '55432',
                '--user',
                'postgres',
                '--database',
                f'pg_perf_bench_{profile_id.replace("-", "_")}',
                '--pg-data-path',
                pgdata,
                '--pg-bin-path',
                f'/usr/lib/postgresql/{server_major}/bin',
                '--workload-profile',
                profile_id,
                '--workload-scale',
                '0.25',
                '--workload-duration-seconds',
                '5',
                '--pgbench-clients',
                '1,2',
                '--command-timeout',
                '300',
                '--out',
                report_dir,
                '--log-dir',
                tmp_path / 'log',
                '--report-name',
                f'{profile_id}-pg{server_major}',
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            accepted_codes=(0, 5),
        )
        report_text = (report_dir / f'{profile_id}-pg{server_major}.json').read_text(
            encoding='utf-8'
        )
        report = json.loads(report_text)
        assert report['workload_evidence']['profile_id'] == profile_id
        assert report['postgresql_compatibility']['server']['major'] == server_major
        assert len(report['benchmark_runs']) == 2
        for run in report['benchmark_runs']:
            assert run['init']['returncode'] == 0
            assert run['workload']['returncode'] == 0
            assert 'failed transactions: 0' in run['workload']['stdout']
            assert run['metrics']['tps'] > 0
        assert password not in report_text
    finally:
        if stand_up:
            _run([*common_stand_args, 'down', '--clear-data'], cwd=stand_root)
