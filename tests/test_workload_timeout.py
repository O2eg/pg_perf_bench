import shlex
from pathlib import Path

import pytest

from pg_perf_bench.errors import ConfigurationError
from pg_perf_bench.workload_timeout import bounded_workload_command, script_arguments


def test_timeout_stages_weighted_script_without_changing_source(tmp_path):
    source = tmp_path / 'query with spaces.sql'
    source.write_text('BEGIN;\nSELECT 1;\nCOMMIT;\n')
    original = source.read_bytes()
    command = shlex.join(['pgbench', '-M', 'prepared', '--file=' + str(source) + '@7', 'db'])
    with bounded_workload_command(command, 0.05) as bounded:
        args = shlex.split(bounded)
        assert args[:3] == ['pgbench', '-M', 'prepared']
        path = Path(args[3].removeprefix('--file=').removesuffix('@7'))
        assert path.read_text().startswith('SET statement_timeout=50;\nBEGIN;')
        assert path.read_text().endswith('RESET statement_timeout;\n')
        assert args[3].endswith('@7')
        assert source.read_bytes() == original
    assert not path.exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize('command', ['pgbench -b tpcb-like db', 'pgbench db', 'pgbench -f'])
def test_sql_timeout_rejects_commands_it_cannot_bound(command):
    with pytest.raises(ConfigurationError):
        script_arguments(command)


def test_no_timeout_preserves_custom_command_verbatim():
    command = 'custom-wrapper --arg "hello world"'
    with bounded_workload_command(command, None) as bounded:
        assert bounded == command
