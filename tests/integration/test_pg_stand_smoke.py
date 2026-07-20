import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_PG_STAND_INTEGRATION') != '1',
        reason=(
            'set PG_PERF_BENCH_PG_STAND_INTEGRATION=1 to provision a disposable '
            'pg_stand environment'
        ),
    ),
]


def _run(arguments, *, cwd, env=None, accepted_codes=(0,)):
    result = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if result.returncode not in accepted_codes:
        raise AssertionError(
            f'command failed ({result.returncode}): {arguments}\n'
            f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        )
    return result


def test_pg_stand_benchmark_smoke(tmp_path):
    pg_stand = Path(
        os.environ.get(
            'PG_STAND_BIN',
            '/home/oleg/Desktop/dev/pg_stand/.venv/bin/pg-stand',
        )
    )
    if not pg_stand.is_file():
        pytest.skip(f'pg-stand executable not found: {pg_stand}')

    stand_root = tmp_path / 'stand'
    _run([pg_stand, 'init', '--directory', stand_root], cwd=tmp_path)
    config = stand_root / 'configs' / 'single.yaml'
    stand_up = False
    try:
        _run([pg_stand, '-c', config, 'up', '--timeout', '180'], cwd=stand_root)
        stand_up = True
        credentials_path = stand_root / '.pg_stand' / 'credentials' / 'database' / 'passwords.json'
        credentials = json.loads(credentials_path.read_text(encoding='utf-8'))
        password = credentials['superuser']['password']
        environment = os.environ.copy()
        environment['PGPASSWORD'] = password
        report_dir = tmp_path / 'report'
        container = 'pg-stand-single-primary-pg-stand-managed'
        result = _run(
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
                '--pg-host',
                '127.0.0.1',
                '--pg-port',
                '55432',
                '--pg-user',
                'postgres',
                '--pg-database',
                'pg_perf_bench_it',
                '--pg-data-path',
                '/var/lib/postgresql/data',
                '--pg-bin-path',
                '/usr/lib/postgresql/17/bin',
                '--benchmark-type',
                'default',
                '--pgbench-clients',
                '1',
                '--pgbench-path',
                f'docker exec -e PGPASSWORD {container} pgbench',
                '--psql-path',
                f'docker exec -e PGPASSWORD {container} psql',
                '--init-command',
                ('ARG_PGBENCH_PATH -i -s 1 -h 127.0.0.1 -p 5432 -U postgres ARG_PG_DATABASE'),
                '--workload-command',
                (
                    'ARG_PGBENCH_PATH -T 1 -c ARG_PGBENCH_CLIENTS -j 1 '
                    '-h 127.0.0.1 -p 5432 -U postgres ARG_PG_DATABASE'
                ),
                '--command-timeout',
                '30',
                '--output-dir',
                report_dir,
                '--log-dir',
                tmp_path / 'log',
                '--report-name',
                'pg-stand-smoke',
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            accepted_codes=(0, 5),
        )
        assert result.returncode in {0, 5}
        report_text = (report_dir / 'pg-stand-smoke.json').read_text(encoding='utf-8')
        report = json.loads(report_text)
        assert report['benchmark_runs'][0]['metrics']['tps'] > 0
        assert password not in report_text
        _run([pg_stand, '-c', config, 'health'], cwd=stand_root)
    finally:
        if stand_up:
            _run(
                [pg_stand, '-c', config, 'down', '--clear-data'],
                cwd=stand_root,
            )
