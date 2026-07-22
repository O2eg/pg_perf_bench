"""Opt-in end-to-end config comparison and JOIN report verification."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('PG_PERF_BENCH_PG_STAND_JOIN') != '1',
        reason='set PG_PERF_BENCH_PG_STAND_JOIN=1 to run config/JOIN integration',
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


def _benchmark_arguments(report_dir: Path, report_name: str) -> list[object]:
    return [
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
        'pg_perf_bench_config_join',
        '--pg-data-path',
        '/var/lib/postgresql/18/docker',
        '--pg-bin-path',
        '/usr/lib/postgresql/18/bin',
        '--benchmark-type',
        'default',
        '--pgbench-clients',
        '1,2',
        '--init-command',
        ('ARG_PGBENCH_PATH -i -s 1 -h 127.0.0.1 -p 55432 -U postgres ARG_PG_DATABASE'),
        '--workload-command',
        (
            'ARG_PGBENCH_PATH -T 2 -c ARG_PGBENCH_CLIENTS '
            '-j ARG_PGBENCH_CLIENTS -h 127.0.0.1 -p 55432 '
            '-U postgres ARG_PG_DATABASE'
        ),
        '--command-timeout',
        '60',
        '--out',
        report_dir,
        '--log-dir',
        report_dir / 'log',
        '--report-name',
        report_name,
    ]


def test_config_change_then_join_stacks_tps_and_cpu_charts(tmp_path):
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
    stand_args = [pg_stand, '-c', config, '--pg-version', '18']
    stand_up = False
    try:
        _run([*stand_args, 'up', '--timeout', '240'], cwd=stand_root)
        stand_up = True
        credentials_path = stand_root / '.pg_stand' / 'credentials' / 'database' / 'passwords.json'
        password = json.loads(credentials_path.read_text(encoding='utf-8'))['superuser']['password']
        environment = os.environ.copy()
        environment['PGPASSWORD'] = password
        report_dir = tmp_path / 'reports'
        _run(
            _benchmark_arguments(report_dir, 'baseline'),
            cwd=Path(__file__).parents[2],
            env=environment,
            accepted_codes=(0, 5),
        )

        document = yaml.safe_load(config.read_text(encoding='utf-8'))
        document['spec']['postgres']['parameters']['shared_buffers'] = '384MB'
        config.write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')
        _run([*stand_args, 'apply', '--restart', '--timeout', '240'], cwd=stand_root)
        _run(
            _benchmark_arguments(report_dir, 'tuned'),
            cwd=Path(__file__).parents[2],
            env=environment,
            accepted_codes=(0, 5),
        )

        joined_dir = tmp_path / 'joined'
        _run(
            [
                sys.executable,
                '-m',
                'pg_perf_bench',
                'join',
                '--join-task',
                'optimize-db-config',
                '--reference-report',
                'baseline.json',
                '--input-dir',
                report_dir,
                '--out',
                joined_dir,
                '--report-name',
                'config-comparison',
            ],
            cwd=Path(__file__).parents[2],
            accepted_codes=(0, 5),
        )
        joined = json.loads((joined_dir / 'config-comparison.json').read_text(encoding='utf-8'))
        tps_series = joined['sections']['result']['reports']['chart']['data']['series']
        assert [series['name'] for series in tps_series] == ['baseline', 'tuned']
        assert len(joined['joined_maximum_tps']) == 2
        cpu_blocks = joined['sections']['os_metrics']['reports']['os_cpu_utilization']['data']
        assert [block['report_name'] for block in cpu_blocks] == [
            'baseline',
            'baseline',
            'tuned',
            'tuned',
        ]
        assert all(block['chart']['series'] for block in cpu_blocks)
    finally:
        if stand_up:
            _run([*stand_args, 'down', '--clear-data'], cwd=stand_root)
