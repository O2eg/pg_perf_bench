"""Run the production sampling commands through pgbench on virtual ID bounds."""

import os
import re
import subprocess

import pytest

from tests.integration.test_imdb_semantics import ROOT
from tests.integration.test_imdb_semantics import pytestmark as semantic_marks

pytestmark = semantic_marks


@pytest.mark.parametrize(
    'script,variable',
    [('05_movie_details.sql', 'movie_id'), ('06_person_filmography.sql', 'credit_id')],
)
@pytest.mark.parametrize('protocol', ['simple', 'prepared'])
@pytest.mark.parametrize('lo,hi', [(1, 800000), (1, 4800242), (1, 18500000), (4800242, 4800242)])
def test_sampler_covers_actual_domain_without_quantization(
    tmp_path, script, variable, protocol, lo, hi
):
    source = (ROOT / 'sql' / script).read_text()
    end = re.search(r'(?m)^\\set ' + variable + r' .*$', source)
    assert end is not None
    prefix = source[: end.end()]
    prefix, count = re.subn(
        r'SELECT min\(id\) AS lo, max\(id\) AS hi FROM (title|cast_info)',
        f'SELECT {lo} AS lo, {hi} AS hi',
        prefix,
    )
    assert count == 1
    probe = tmp_path / 'sampling.sql'
    probe.write_text(prefix + f'\n\\set observed debug(:{variable})\n')
    result = subprocess.run(
        [
            '/usr/bin/pgbench',
            '-n',
            '-M',
            protocol,
            '-c',
            '1',
            '-t',
            '256',
            '--random-seed=42',
            '-f',
            str(probe),
        ],
        env=os.environ,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    values = [int(v) for v in re.findall(r'debug[^\n]*:\s*int (\d+)', result.stderr)]
    assert len(values) == 256, result.stderr
    assert all(lo <= value <= hi for value in values)
    if lo == hi:
        assert set(values) == {hi}  # The formerly unreachable final credit must be selectable.
    else:
        assert min(values) < lo + (hi - lo) / 8 and max(values) > hi - (hi - lo) / 8
        buckets = [sum((v - lo) * 8 // (hi - lo + 1) == i for v in values) for i in range(8)]
        assert all(10 <= n <= 60 for n in buckets), buckets
        if hi > 1000000:
            # A value missed by the old million-ticket mapping must be reachable now.
            def old_mapping_can_select(value):
                ticket = ((value - 1) * 1000000 + hi - 1) // hi
                return ticket < 1000000 and 1 + ticket * hi // 1000000 == value

            assert any(not old_mapping_can_select(value) for value in values)
