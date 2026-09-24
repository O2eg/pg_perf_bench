"""Distribution regressions at scales that collide with the original multipliers."""

import json
import math
import re

import pytest

from pg_perf_bench.const import WORKLOAD_PROFILES_PATH
from pg_perf_bench.initialization import load_plan

ROOT = WORKLOAD_PROFILES_PATH / 'imdb'


@pytest.mark.parametrize('scale', [0.001, 0.01, 0.08191, 0.16382, 0.32771, 0.65537, 1, 2, 10, 50])
def test_modular_relationships_cover_the_whole_title_domain(scale):
    plan = load_plan(ROOT, 'generator.py', scale)
    checked = 0
    for task in plan.data:
        for stride, size in re.findall(r'g::bigint \* (\d+) - 1\) % (\d+)', task.sql):
            stride, size = int(stride), int(size)
            assert math.gcd(stride, size) == 1, (task.name, scale, stride, size)
            if size <= 65537:
                assert len({1 + ((g * stride - 1) % size) for g in range(1, size + 1)}) == size
            checked += 1
    assert checked >= 4


def test_legacy_installs_all_fast_secondary_indexes():
    plan = load_plan(ROOT, 'generator.py', 1)
    legacy = re.sub(r'\s+', '', (ROOT / 'sql/imdb-fkindexes.sql').read_text()).lower()
    for task in plan.indexes:
        if not task.name.endswith('_pkey'):
            assert re.sub(r'\s+', '', task.sql).lower() in legacy, task.name


def test_application_scripts_are_atomic_and_planner_set_is_separate():
    profile = json.loads((ROOT / 'profile.json').read_text())
    assert len(profile['files']['queries']) == 11
    assert len(profile['files']['planner_queries']) == 118
    for role in ('queries', 'planner_queries'):
        for filename in profile['files'][role]:
            sql = (ROOT / filename).read_text()
            # Remove comments/meta commands before checking top-level SQL boundaries.
            sql = sql.replace(r'\gset bounds_', ';')
            sql = re.sub(r'(?m)^\\.*$|--[^\n]*', '', sql)
            statements = [part.strip() for part in sql.split(';') if part.strip()]
            # SET, optional bounds lookup, one business SELECT.
            sampled = filename.endswith(('05_movie_details.sql', '06_person_filmography.sql'))
            assert len(statements) == (3 if sampled else 2), filename
            assert statements[0].upper().startswith('SET SEARCH_PATH')
            assert statements[1].upper().startswith(('SELECT', 'WITH'))
    assert all('/planner/' not in path for path in profile['files']['queries'])
