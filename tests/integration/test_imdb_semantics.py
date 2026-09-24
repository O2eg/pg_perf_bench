"""Opt-in semantic checks on an initialized disposable IMDb database."""

import asyncio
import json
import os
import re
import subprocess
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import asyncpg
import pytest

from pg_perf_bench.const import WORKLOAD_PROFILES_PATH

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get('IMDB_PROFILE_VALIDATION') != '1', reason='disposable IMDb DB required'
    ),
]
ROOT = Path(os.environ.get('IMDB_PROFILE_PATH', str(WORKLOAD_PROFILES_PATH / 'imdb')))


def planner_source(stem):
    return '\n'.join(p.read_text() for p in sorted((ROOT / 'sql/planner').glob(stem + '_*.sql')))


async def connect():
    assert os.environ['PGDATABASE'].startswith('imdb_profile_validation_')
    conn = await asyncpg.connect(
        host=os.environ['PGHOST'],
        port=int(os.environ['PGPORT']),
        user=os.environ['PGUSER'],
        password=os.environ['PGPASSWORD'],
        database=os.environ['PGDATABASE'],
        ssl=False,
        command_timeout=60,
    )
    await conn.execute("SET search_path=imdb,public; SET statement_timeout='60s'")
    return conn


def check_async(callback):
    async def run():
        conn = await connect()
        try:
            await conn.execute('BEGIN READ ONLY')
            await callback(conn)
        finally:
            await conn.execute('ROLLBACK')
            await conn.close()

    asyncio.run(run())


def test_all_titles_have_consistent_attributes_and_coverage():
    async def run(c):
        n = await c.fetchval('SELECT count(*) FROM title')
        for table in (
            'cast_info',
            'movie_keyword',
            'movie_companies',
            'movie_info',
            'movie_info_idx',
        ):
            assert await c.fetchval(f'SELECT count(DISTINCT movie_id) FROM {table}') == n
        for kind in (3, 5):
            assert (
                await c.fetchval('SELECT count(*) FROM movie_info_idx WHERE info_type_id=$1', kind)
                == n
            )
        assert await c.fetchval('SELECT count(*) FROM movie_info_idx WHERE info_type_id=1') == 250
        assert await c.fetchval('SELECT count(*) FROM movie_info_idx WHERE info_type_id=2') == 10
        assert not await c.fetchval("""SELECT EXISTS (SELECT movie_id FROM movie_info_idx
            WHERE info_type_id IN (1,2) GROUP BY movie_id HAVING count(*)>1)""")
        assert not await c.fetchval("""SELECT EXISTS (
            SELECT movie_id,info_type_id FROM movie_info_idx
            GROUP BY 1,2 HAVING count(*)>1)""")
        assert await c.fetchval('SELECT count(DISTINCT kind_id) FROM title') == 7
        assert (
            await c.fetchval("""SELECT min(n) FROM (SELECT count(*) n FROM company_name
            GROUP BY country_code) x""")
            > 1
        )
        for kind, direction in ((1, 'DESC'), (2, 'ASC')):
            tie = '' if kind == 1 else ' DESC'
            rows = await c.fetch(f"""SELECT r.movie_id FROM movie_info_idx r
                JOIN movie_info_idx v USING(movie_id) JOIN title t ON t.id=r.movie_id
                WHERE r.info_type_id=3 AND v.info_type_id=5 AND t.kind_id=1
                ORDER BY r.info::numeric {direction},v.info::numeric {direction},r.movie_id{tie}
                LIMIT {250 if kind == 1 else 10}""")
            ranked = await c.fetch(
                """SELECT movie_id FROM movie_info_idx
                WHERE info_type_id=$1 ORDER BY info::numeric""",
                kind,
            )
            assert rows == ranked

    check_async(run)


def test_temporal_episode_and_link_invariants():
    async def run(c):
        queries = [
            """SELECT count(*) FROM movie_info mi JOIN title t ON t.id=mi.movie_id
            WHERE mi.info_type_id=8 AND substring(mi.info FROM
            '[0-9]{4}')::integer<t.production_year""",
            """SELECT count(*) FROM movie_companies mc JOIN title t ON t.id=mc.movie_id
            CROSS JOIN LATERAL regexp_matches(mc.note, '[(]([12][0-9]{3})[)]', 'g')
            y WHERE y[1]::integer<t.production_year""",
            """SELECT count(*) FROM title WHERE kind_id=3 AND (episode_of_id IS NULL OR
            season_nr IS NULL OR episode_nr IS NULL)""",
            """SELECT count(*) FROM title WHERE kind_id<>3 AND (episode_of_id IS NOT
            NULL OR season_nr IS NOT NULL OR episode_nr IS NOT NULL)""",
            """SELECT count(*) FROM title e JOIN title p ON p.id=e.episode_of_id WHERE
            p.kind_id<>2 OR e.production_year<p.production_year OR e.season_nr<1 OR
            e.episode_nr<1""",
            'SELECT count(*) FROM movie_link WHERE movie_id=linked_movie_id',
            """SELECT count(*) FROM movie_link l JOIN title a ON a.id=l.movie_id JOIN
            title b ON b.id=l.linked_movie_id WHERE (l.link_type_id IN (1,2,4,6) AND
            a.production_year<b.production_year) OR (l.link_type_id IN (3,5,7) AND
            a.production_year>b.production_year)""",
            """SELECT count(*) FROM aka_title a JOIN title t ON t.id=a.movie_id WHERE
            (a.kind_id,a.production_year,a.episode_of_id,a.season_nr,a.episode_nr)
            IS DISTINCT FROM
            (t.kind_id,t.production_year,t.episode_of_id,t.season_nr,t.episode_nr)""",
            'SELECT count(*) FROM cast_info WHERE role_id>2 AND person_role_id IS NOT NULL',
        ]
        for query in queries:
            assert await c.fetchval(query) == 0, query

    check_async(run)


def test_generated_references_are_not_orphaned():
    async def run(c):
        refs = {
            'aka_name': {'person_id': 'name'},
            'aka_title': {'movie_id': 'title', 'kind_id': 'kind_type'},
            'cast_info': {
                'person_id': 'name',
                'movie_id': 'title',
                'person_role_id': 'char_name',
                'role_id': 'role_type',
            },
            'complete_cast': {
                'movie_id': 'title',
                'subject_id': 'comp_cast_type',
                'status_id': 'comp_cast_type',
            },
            'movie_companies': {
                'movie_id': 'title',
                'company_id': 'company_name',
                'company_type_id': 'company_type',
            },
            'movie_info': {'movie_id': 'title', 'info_type_id': 'info_type'},
            'movie_info_idx': {'movie_id': 'title', 'info_type_id': 'info_type'},
            'movie_keyword': {'movie_id': 'title', 'keyword_id': 'keyword'},
            'movie_link': {
                'movie_id': 'title',
                'linked_movie_id': 'title',
                'link_type_id': 'link_type',
            },
            'person_info': {'person_id': 'name', 'info_type_id': 'info_type'},
            'title': {'kind_id': 'kind_type', 'episode_of_id': 'title'},
        }
        for table, columns in refs.items():
            for column, parent in columns.items():
                assert not await c.fetchval(f"""SELECT EXISTS (SELECT 1 FROM {table} a
                    WHERE a.{column} IS NOT NULL AND NOT EXISTS (
                        SELECT FROM {parent} b WHERE b.id=a.{column}))"""), (table, column)

    check_async(run)


def test_company_average_is_per_title_not_per_company_credit():
    async def run(c):
        raw = await c.fetch("""SELECT cn.id,cn.name,t.id AS movie_id,t.production_year,r.info
            FROM company_name cn JOIN movie_companies mc ON mc.company_id=cn.id
            JOIN title t ON t.id=mc.movie_id
            JOIN movie_info_idx r ON r.movie_id=t.id AND r.info_type_id=3""")
        groups = defaultdict(dict)
        for row in raw:
            rating = Decimal(row['info'])
            if 2000 <= row['production_year'] <= 2022 and rating >= 6:
                groups[(row['id'], row['name'], row['production_year'])][row['movie_id']] = rating
        expected = []
        for (company_id, name, year), movies in groups.items():
            average = (sum(movies.values()) / len(movies)).quantize(
                Decimal('.01'), rounding=ROUND_HALF_UP
            )
            expected.append((name, year, len(movies), average, company_id))
        expected.sort(key=lambda r: (-r[2], -r[3], r[4], r[1]))
        sql = (ROOT / 'sql/01_company_catalog.sql').read_text().split(';', 1)[1].strip().rstrip(';')
        actual = await c.fetch(sql)
        assert [tuple(row) for row in actual] == [row[:4] for row in expected[:50]]

    check_async(run)


@pytest.mark.parametrize('protocol', ['simple', 'prepared'])
def test_all_pgbench_scripts(protocol):
    # Every script is explicitly executed; random script selection cannot hide a broken one.
    manifest = json.loads((ROOT / 'profile.json').read_text())
    for name in manifest['files']['queries']:
        script = ROOT / name
        result = subprocess.run(
            [
                '/usr/bin/pgbench',
                '-n',
                '-M',
                protocol,
                '-c',
                '1',
                '-j',
                '1',
                '-t',
                '6' if protocol == 'prepared' else '1',
                '-f',
                str(script),
            ],
            env={**os.environ, 'PGOPTIONS': '-c statement_timeout=60000'},
            text=True,
            capture_output=True,
            timeout=180,
        )
        assert result.returncode == 0, (script.name, result.stderr)
        assert 'number of failed transactions: 0' in result.stdout, script.name


def test_budget_and_votes_are_numeric_minima():
    async def run(c):
        source = planner_source('select_18')
        query = next(q.strip() for q in source.split(';') if q.lstrip().startswith('SELECT'))
        row = await c.fetchrow(query)
        raw = await c.fetch("""SELECT mi.info AS budget, v.info AS votes
            FROM cast_info ci JOIN name n ON n.id=ci.person_id
            JOIN movie_info mi ON mi.movie_id=ci.movie_id AND mi.info_type_id=6
            JOIN movie_info_idx v ON v.movie_id=ci.movie_id AND v.info_type_id=5
            WHERE ci.note IN ('(producer)','(executive producer)')
              AND n.gender='m' AND n.name LIKE '%Tim%'""")
        assert raw
        assert row['movie_budget'] == min(Decimal(r['budget'][1:]) for r in raw)
        assert row['movie_votes'] == min(Decimal(r['votes']) for r in raw)

    check_async(run)


def test_producer_query_uses_person_without_requiring_a_character():
    async def run(c):
        source = planner_source('select_10')
        queries = [q.strip() for q in source.split(';') if q.lstrip().startswith('SELECT')]
        row = await c.fetchrow(queries[2])
        credits = await c.fetch("""SELECT DISTINCT n.name,t.title
            FROM name n JOIN cast_info ci ON ci.person_id=n.id
            JOIN title t ON t.id=ci.movie_id
            WHERE ci.role_id=5 AND t.production_year>1990 AND EXISTS (
                SELECT FROM movie_companies mc JOIN company_name cn ON cn.id=mc.company_id
                WHERE mc.movie_id=t.id AND cn.country_code='[us]')""")
        assert credits
        assert row['producer'] == min(r['name'] for r in credits)
        assert row['movie_with_american_producer'] == min(r['title'] for r in credits)

    check_async(run)


def application_variants(source, bounds=None):
    if r'\gset bounds_' in source:
        match = re.search(
            r'SELECT min\(id\) AS lo, max\(id\) AS hi FROM (\w+)\n\\gset bounds_', source
        )
        assert match is not None and bounds is not None
        low, high = bounds[match[1]]
        source = source[: match.start()] + source[match.end() :]
        source = source.replace(':bounds_lo', str(low)).replace(':bounds_hi', str(high))
    matches = re.findall(r'\\set (\w+) random\((\d+),\s*(\d+)\)', source)
    sql = '\n'.join(line for line in source.splitlines() if not line.startswith('\\set'))
    sql = sql.split(';', 1)[1].strip().rstrip(';')
    if not matches:
        yield {}, sql
    else:
        assert len(matches) == 1
        name, low, high = matches[0]
        low, high = int(low), int(high)
        values = range(low, high + 1) if high - low < 100 else (low, (low + high) // 2, high)
        for value in values:
            yield {name: value}, re.sub(r'(?<!:):(\w+)', lambda m, value=value: str(value), sql)


def test_every_application_scenario_returns_useful_data():
    async def run(c):
        profile = json.loads((ROOT / 'profile.json').read_text())
        bounds = {
            table: tuple(await c.fetchrow(f'SELECT min(id),max(id) FROM {table}'))
            for table in ('title', 'cast_info')
        }
        for filename in profile['files']['queries']:
            for parameters, sql in application_variants((ROOT / filename).read_text(), bounds):
                rows = await c.fetch(sql)
                assert rows and any(v is not None for row in rows for v in row.values()), (
                    filename,
                    parameters,
                )

    check_async(run)


def test_generator_matches_catalog_predicates_in_scaled_background():
    async def run(c):
        assert await c.fetchval("SELECT count(*) FROM name WHERE id>8 AND name LIKE '%Tim%'") > 1
        assert await c.fetchval("""SELECT EXISTS(SELECT FROM movie_companies mc
            JOIN title t ON t.id=mc.movie_id JOIN movie_info mi ON mi.movie_id=t.id
            WHERE mc.company_type_id=1 AND mc.note LIKE '%(VHS)%'
            AND mc.note LIKE '%(1994)%' AND mc.note LIKE '%(USA)%'
            AND t.production_year BETWEEN 1990 AND 1994
            AND mi.info_type_id=7 AND mi.info='USA')""")
        assert (
            await c.fetchval(
                'SELECT info::numeric FROM movie_info_idx WHERE movie_id=7 AND info_type_id=3'
            )
            >= 6
        )
        assert await c.fetchval("""SELECT EXISTS(SELECT FROM movie_link
            WHERE movie_id=21 AND linked_movie_id=19 AND link_type_id=1)""")
        for predicate, keyword in [("t.title LIKE 'Murder%'", 13), ("t.title LIKE 'Money%'", 2)]:
            assert (
                await c.fetchval(f'SELECT count(*) FROM title t WHERE t.id>22 AND {predicate}') > 1
            )
            assert not await c.fetchval(f"""SELECT EXISTS(SELECT FROM title t
                WHERE t.id>22 AND {predicate} AND NOT EXISTS(SELECT FROM movie_keyword mk
                WHERE mk.movie_id=t.id AND mk.keyword_id={keyword}))""")
        for role in (1, 3):
            predicate = 'role_id IN (1,2)' if role == 1 else 'role_id=3'
            assert not await c.fetchval(f"""SELECT EXISTS(SELECT FROM title t WHERE NOT EXISTS
                (SELECT FROM cast_info ci WHERE ci.movie_id=t.id AND {predicate}))""")
        assert not await c.fetchval("""SELECT EXISTS(SELECT FROM title t WHERE NOT EXISTS
            (SELECT FROM movie_companies mc WHERE mc.movie_id=t.id AND mc.company_type_id=1))""")
        assert not await c.fetchval("""SELECT EXISTS(SELECT FROM cast_info ci JOIN name n
            ON n.id=ci.person_id WHERE (ci.role_id=1 AND n.gender<>'m')
            OR (ci.role_id=2 AND n.gender<>'f'))""")
        if await c.fetchval('SELECT count(*) FROM title') >= 30000:
            for filename in ('09_vhs_archive.sql', '11_movie_links.sql'):
                _, sql = next(application_variants((ROOT / 'sql' / filename).read_text()))
                rows = await c.fetch(sql)
                assert any(row['id'] > 22 for row in rows), filename

    check_async(run)


def test_exists_rewrites_preserve_original_planner_results():
    reference = json.loads(
        (Path(__file__).parents[1] / 'fixtures/imdb_planner_reference.json').read_text()
    )

    async def run(c):
        for filename, old_sql in reference.items():
            new_sql = (
                (ROOT / 'sql/planner' / filename).read_text().split(';', 1)[1].strip().rstrip(';')
            )
            assert await c.fetch(new_sql) == await c.fetch(old_sql), filename

    check_async(run)


def test_sampled_id_domains_are_dense():
    async def run(c):
        for table in ('title', 'cast_info'):
            n, lo, hi = await c.fetchrow(f'SELECT count(*), min(id), max(id) FROM {table}')
            assert n > 0 and n == hi - lo + 1, table

    check_async(run)


def test_release_events_have_independent_calendar_dates():
    async def run(c):
        assert not await c.fetchval("""SELECT EXISTS(SELECT FROM movie_companies
            WHERE note LIKE '%(VHS)%' AND note LIKE '%(Blu-ray)%')""")
        assert not await c.fetchval("""SELECT EXISTS(SELECT FROM movie_companies mc
            JOIN title t ON t.id=mc.movie_id WHERE mc.note IS NOT NULL AND (
              substring(mc.note FROM '[(]([12][0-9]{3})[)]') IS NULL OR
              substring(mc.note FROM '[(]([12][0-9]{3})[)]')::int<t.production_year OR
              substring(mc.note FROM '[(]([12][0-9]{3})[)]')::int>2024 OR
              (mc.note LIKE '%(VHS)%' AND substring(mc.note FROM
                 '[(]([12][0-9]{3})[)]')::int NOT BETWEEN 1980 AND 2005) OR
              (mc.note LIKE '%(Blu-ray)%' AND substring(mc.note FROM
                 '[(]([12][0-9]{3})[)]')::int<2006)))""")
        assert not await c.fetchval("""SELECT EXISTS(SELECT FROM movie_companies mc
            WHERE mc.note IS NOT NULL AND (SELECT count(*) FROM regexp_matches(
                mc.note,'[(]([12][0-9]{3})[)]','g'))<>1)""")
        for year in range(1990, 1995):
            assert await c.fetchval(
                """SELECT EXISTS(SELECT FROM title t
                JOIN movie_companies mc ON mc.movie_id=t.id
                WHERE t.id>22 AND t.production_year=$1 AND mc.company_type_id=1
                AND mc.note='(VHS) (USA) (1994)' AND EXISTS(SELECT FROM movie_info mi
                    WHERE mi.movie_id=t.id AND mi.info_type_id=7 AND mi.info='USA'))""",
                year,
            )
        _, sql = next(application_variants((ROOT / 'sql/09_vhs_archive.sql').read_text()))
        rows = await c.fetch(sql)
        assert any(row['production_year'] < 1994 for row in rows)
        assert all(row['vhs_release_year'] == 1994 for row in rows)

    check_async(run)
