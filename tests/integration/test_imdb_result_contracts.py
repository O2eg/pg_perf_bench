"""Independent Python oracle for every application result on bounded debug datasets."""

import json
import re
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

import pytest

from tests.integration.test_imdb_semantics import ROOT, application_variants, check_async
from tests.integration.test_imdb_semantics import pytestmark as semantic_marks

pytestmark = semantic_marks


def test_application_results_against_source_records():
    async def run(c):
        if await c.fetchval('SELECT count(*) FROM title') > 50000:
            pytest.skip('Python oracle intentionally bounded to 50,000 titles')
        tables = {}
        for table in (
            'title',
            'name',
            'cast_info',
            'movie_info',
            'movie_info_idx',
            'movie_keyword',
            'keyword',
            'movie_companies',
            'company_name',
            'role_type',
            'complete_cast',
            'comp_cast_type',
            'movie_link',
            'link_type',
        ):
            tables[table] = [dict(row) for row in await c.fetch('SELECT * FROM ' + table)]
        titles = {r['id']: r for r in tables['title']}
        people = {r['id']: r for r in tables['name']}
        # Respect the database collation; Python's Unicode order may differ.
        name_order = {
            r['id']: i for i, r in enumerate(await c.fetch('SELECT id FROM name ORDER BY name,id'))
        }
        keywords = {r['id']: r['keyword'] for r in tables['keyword']}
        companies = {r['id']: r['name'] for r in tables['company_name']}
        roles = {r['id']: r['role'] for r in tables['role_type']}
        statuses = {r['id']: r['kind'] for r in tables['comp_cast_type']}
        links = {r['id']: r['link'] for r in tables['link_type']}
        attrs = defaultdict(list)
        indexed = {}
        for r in tables['movie_info']:
            attrs[r['movie_id'], r['info_type_id']].append(r['info'])
        for r in tables['movie_info_idx']:
            indexed[r['movie_id'], r['info_type_id']] = r['info']

        def rating(mid):
            return Decimal(indexed[mid, 3])

        def votes(mid):
            return int(indexed[mid, 5])

        def year(mid):
            return titles[mid]['production_year']

        def title_row(mid):
            return (mid, titles[mid]['title'], year(mid))

        cast_by_title, cast_by_person = defaultdict(list), defaultdict(list)
        for r in tables['cast_info']:
            cast_by_title[r['movie_id']].append(r)
            cast_by_person[r['person_id']].append(r)
        kw_by_title = defaultdict(set)
        for r in tables['movie_keyword']:
            kw_by_title[r['movie_id']].add(r['keyword_id'])
        company_by_title = defaultdict(list)
        for r in tables['movie_companies']:
            company_by_title[r['movie_id']].append(r)

        def expected(number, params):
            if number == 1:
                groups = defaultdict(dict)
                for mid, credits in company_by_title.items():
                    if 2000 <= year(mid) <= 2022 and rating(mid) >= 6:
                        for credit in credits:
                            groups[credit['company_id'], year(mid)][mid] = rating(mid)
                rows = []
                for (cid, yr), movies in groups.items():
                    avg = (sum(movies.values()) / len(movies)).quantize(
                        Decimal('.01'), rounding=ROUND_HALF_UP
                    )
                    rows.append((companies[cid], yr, len(movies), avg, cid))
                rows.sort(key=lambda r: (-r[2], -r[3], r[4], r[1]))
                return [r[:4] for r in rows[:50]]
            if number == 2:
                kid = params['keyword_id']
                groups = defaultdict(set)
                for mid, kids in kw_by_title.items():
                    if kid in kids and 2000 <= year(mid) <= 2022:
                        for credit in cast_by_title[mid]:
                            if credit['role_id'] in (1, 2):
                                groups[credit['person_id']].add(mid)
                rows = [
                    (
                        pid,
                        people[pid]['name'],
                        keywords[kid],
                        len(mids),
                        min(map(year, mids)),
                        max(map(year, mids)),
                    )
                    for pid, mids in groups.items()
                ]
                return sorted(rows, key=lambda r: (-r[3], -r[5], r[0]))[:100]
            if number == 3:
                groups = defaultdict(set)
                for mid, kids in kw_by_title.items():
                    if year(mid) >= 1980:
                        for kid in kids:
                            groups[year(mid) // 5 * 5, kid].add(mid)
                rows = [
                    (
                        bucket,
                        keywords[kid],
                        len(mids),
                        len({titles[mid]['kind_id'] for mid in mids}),
                        kid,
                    )
                    for (bucket, kid), mids in groups.items()
                ]
                return [r[:4] for r in sorted(rows, key=lambda r: (-r[2], r[0], r[4]))[:100]]
            if number in (4, 13):
                genre = ['Horror', 'Thriller', 'Action', 'Sci-Fi'][params['genre_no'] - 1]
                mids = {
                    mid for mid in titles if 2010 <= year(mid) <= 2022 and genre in attrs[mid, 4]
                }
                if number == 13:
                    mids = [
                        mid
                        for mid in mids
                        if any(credit['role_id'] == 3 for credit in cast_by_title[mid])
                    ]
                    return [
                        title_row(mid) + (votes(mid), rating(mid))
                        for mid in sorted(mids, key=lambda mid: (-votes(mid), mid))[:20]
                    ]
                groups = defaultdict(list)
                for mid in mids:
                    for credit in cast_by_title[mid]:
                        groups[credit['role_id']].append(credit)
                rows = [
                    (
                        roles[rid],
                        len(credits),
                        len({r['person_id'] for r in credits}),
                        len({r['movie_id'] for r in credits}),
                        rid,
                    )
                    for rid, credits in groups.items()
                ]
                return [r[:4] for r in sorted(rows, key=lambda r: (-r[1], r[4]))]
            if number == 5:
                mid = params['movie_id']
                credits = [
                    {
                        'id': r['person_id'],
                        'name': people[r['person_id']]['name'],
                        'role': roles[r['role_id']],
                    }
                    for r in sorted(cast_by_title[mid], key=lambda r: (r['nr_order'], r['id']))
                ]
                kids = [keywords[kid] for kid in sorted(kw_by_title[mid])]
                cids = sorted({r['company_id'] for r in company_by_title[mid]})
                return [
                    title_row(mid)
                    + (
                        rating(mid),
                        attrs[mid, 6][0],
                        credits,
                        kids,
                        [companies[cid] for cid in cids],
                    )
                ]
            if number == 6:
                picked = next(
                    r['person_id'] for r in tables['cast_info'] if r['id'] == params['credit_id']
                )
                groups = defaultdict(set)
                for credit in cast_by_person[picked]:
                    groups[credit['movie_id']].add(roles[credit['role_id']])
                return [
                    title_row(mid)
                    + (picked, people[picked]['name'], sorted(groups[mid]), rating(mid))
                    for mid in sorted(groups, key=lambda mid: (-year(mid), mid))[:100]
                ]
            if number in (7, 8):
                reverse = number == 7
                mids = [mid for mid in titles if titles[mid]['kind_id'] == 1]
                mids.sort(key=lambda mid: (rating(mid), votes(mid), -mid), reverse=reverse)
                return [
                    (i, *title_row(mid), rating(mid), votes(mid))
                    for i, mid in enumerate(mids[: 50 if reverse else 10], 1)
                ]
            if number == 9:
                mids = [
                    mid
                    for mid in titles
                    if 1990 <= year(mid) <= 1994
                    and 'USA' in attrs[mid, 7]
                    and any(
                        r['company_type_id'] == 1
                        and all(tag in (r['note'] or '') for tag in ('(VHS)', '(USA)', '(1994)'))
                        for r in company_by_title[mid]
                    )
                ]
                return [title_row(mid) + (1994,) for mid in sorted(mids)[:100]]
            if number == 10:
                countries = {'USA', 'Sweden', 'Norway', 'Germany', 'Denmark'}
                mids = [
                    mid
                    for mid in titles
                    if titles[mid]['title'].startswith('Murder')
                    and 2010 <= year(mid) <= 2022
                    and rating(mid) >= 6
                    and kw_by_title[mid] & {13, 14}
                    and countries.intersection(attrs[mid, 7])
                ]
                return [
                    title_row(mid) + (rating(mid),)
                    for mid in sorted(mids, key=lambda mid: (-rating(mid), mid))[:50]
                ]
            if number == 11:
                rows = [
                    r
                    for r in tables['movie_link']
                    if r['link_type_id'] in (1, 2)
                    and titles[r['movie_id']]['title'].startswith('Money')
                ]
                rows.sort(key=lambda r: (r['movie_id'], r['link_type_id'], r['linked_movie_id']))
                return [
                    title_row(r['movie_id'])
                    + (links[r['link_type_id']],)
                    + title_row(r['linked_movie_id'])
                    for r in rows[:100]
                ]
            if number == 12:
                groups = defaultdict(set)
                for r in tables['complete_cast']:
                    if 2010 <= year(r['movie_id']) <= 2022:
                        groups[year(r['movie_id']), r['subject_id'], r['status_id']].add(
                            r['movie_id']
                        )
                return [
                    (yr, statuses[subject], statuses[status], len(groups[yr, subject, status]))
                    for yr, subject, status in sorted(groups)
                ]
            if number == 14:
                pattern = ['Downey.*Robert.*', '.*Tim.*', '.*Angela.*', '.*Alice.*'][
                    params['first_no'] - 1
                ]
                pids = sorted(
                    (pid for pid in people if re.fullmatch(pattern, people[pid]['name'])),
                    key=lambda pid: name_order[pid],
                )[:50]
                rows = []
                for pid in pids:
                    mids = {r['movie_id'] for r in cast_by_person[pid]}
                    if mids:
                        rows.append(
                            (
                                pid,
                                people[pid]['name'],
                                len(mids),
                                min(map(year, mids)),
                                max(map(year, mids)),
                            )
                        )
                return rows
            raise AssertionError(number)

        bounds = {
            table: (min(r['id'] for r in tables[table]), max(r['id'] for r in tables[table]))
            for table in ('title', 'cast_info')
        }
        manifest = json.loads((ROOT / 'profile.json').read_text())
        for filename in manifest['files']['queries']:
            number = int(filename.split('/')[-1][:2])
            for parameters, sql in application_variants((ROOT / filename).read_text(), bounds):
                rows = await c.fetch(sql)
                actual = [
                    tuple(
                        json.loads(value) if number == 5 and i >= 5 else value
                        for i, value in enumerate(row.values())
                    )
                    for row in rows
                ]
                assert actual == expected(number, parameters), (filename, parameters, actual[:2])

    check_async(run)
