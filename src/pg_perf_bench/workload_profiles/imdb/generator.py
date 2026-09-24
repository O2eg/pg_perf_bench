from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
from pathlib import Path

from pg_perf_bench.initialization import LoadOptions, LoadPlan, LoadTask, read_sql_tasks, run_tasks


def scaled(base: int, scale: float, minimum: int) -> int:
    return max(minimum, round(base * scale))


def coprime_stride(preferred: int, cardinality: int) -> int:
    """A modular permutation must visit every key, including fractional scales."""
    while math.gcd(preferred, cardinality) != 1:
        preferred += 1
    return preferred


def build_load_plan(scale: float) -> LoadPlan:
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('scale must be a finite number greater than zero')
    companies = scaled(10_000, scale, 100)
    people = scaled(100_000, scale, 1_000)
    characters = scaled(50_000, scale, 1_000)
    keywords = scaled(20_000, scale, 500)
    titles = scaled(100_000, scale, 1_000)
    cast_rows = scaled(600_000, scale, titles * 3)
    keyword_rows = scaled(300_000, scale, titles * 2)
    company_rows = scaled(150_000, scale, titles)
    info_rows = titles * 4
    info_index_rows = titles * 2
    movie_links = scaled(100_000, scale, titles)

    cast_stride = coprime_stride(65537, titles)
    company_stride = coprime_stride(32771, titles)
    info_stride = coprime_stride(8191, titles)

    data = (
        LoadTask(
            'kind_type',
            """
INSERT INTO kind_type (id, kind) VALUES
            (1, 'movie'),
            (2, 'tv series'),
            (3, 'episode'),
            (4, 'short'),
            (5, 'tv movie'),
            (6, 'video movie'),
            (7, 'video game');
        """,
        ),
        LoadTask(
            'company_type',
            """
INSERT INTO company_type (id, kind) VALUES
            (1, 'production companies'),
            (2, 'distributors'),
            (3, 'special effects companies'),
            (4, 'miscellaneous companies');
        """,
        ),
        LoadTask(
            'info_type',
            """
INSERT INTO info_type (id, info) VALUES
            (1, 'top 250 rank'),
            (2, 'bottom 10 rank'),
            (3, 'rating'),
            (4, 'genres'),
            (5, 'votes'),
            (6, 'budget'),
            (7, 'countries'),
            (8, 'release dates'),
            (9, 'mini biography'),
            (10, 'trivia'),
            (11, 'height'),
            (12, 'runtimes');
        """,
        ),
        LoadTask(
            'link_type',
            """
INSERT INTO link_type (id, link) VALUES
            (1, 'sequel'),
            (2, 'follows'),
            (3, 'followed by'),
            (4, 'features'),
            (5, 'featured in'),
            (6, 'references'),
            (7, 'referenced in');
        """,
        ),
        LoadTask(
            'role_type',
            """
INSERT INTO role_type (id, role) VALUES
            (1, 'actor'),
            (2, 'actress'),
            (3, 'writer'),
            (4, 'costume designer'),
            (5, 'producer'),
            (6, 'director');
        """,
        ),
        LoadTask(
            'comp_cast_type',
            """
INSERT INTO comp_cast_type (id, kind) VALUES
            (1, 'cast'),
            (2, 'crew'),
            (3, 'complete'),
            (4, 'complete+verified');
        """,
        ),
        LoadTask(
            'company_name',
            """
INSERT INTO company_name
            (id, name, country_code, imdb_id, name_pcode_nf, name_pcode_sf, md5sum)
        SELECT
            g,
            CASE g
                WHEN 1 THEN 'Lionsgate Synthetic'
                WHEN 2 THEN 'DreamWorks Animation'
                WHEN 3 THEN 'Warner Film Synthetic'
                WHEN 4 THEN '20th Century Fox Synthetic'
                WHEN 5 THEN 'Twentieth Century Fox Synthetic'
                WHEN 6 THEN 'Synthetic Japan Studio'
                WHEN 7 THEN 'Synthetic German Studio'
                WHEN 8 THEN 'Synthetic Netherlands Studio'
                WHEN 9 THEN 'Synthetic San Marino Studio'
                WHEN 10 THEN 'Synthetic Russian Studio'
                WHEN 11 THEN 'Synthetic Money Film'
                WHEN 12 THEN 'YouTube'
                WHEN 13 THEN 'Synthetic Warner Film'
                ELSE 'Synthetic Company ' || g
            END,
            CASE g
                WHEN 3 THEN '[pl]'
                WHEN 6 THEN '[jp]'
                WHEN 7 THEN '[de]'
                WHEN 8 THEN '[nl]'
                WHEN 9 THEN '[sm]'
                WHEN 10 THEN '[ru]'
                ELSE (ARRAY['[us]', '[us]', '[us]', '[us]', '[us]', '[de]',
                            '[jp]', '[pl]', '[nl]', '[sm]', '[ru]'])[1 + g % 11]
            END,
            100000 + g,
            left(md5('company-nf-' || g), 5),
            left(md5('company-sf-' || g), 5),
            md5('company-' || g)
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=companies,
        ),
        LoadTask(
            'name',
            """
INSERT INTO name
            (id, name, imdb_index, imdb_id, gender, name_pcode_cf, name_pcode_nf,
             surname_pcode, md5sum)
        SELECT
            g,
            CASE g
                WHEN 1 THEN 'Downey Synthetic Robert'
                WHEN 2 THEN 'Angela Synthetic Actress'
                WHEN 3 THEN 'Yoko Synthetic Voice'
                WHEN 4 THEN 'Tim Synthetic Writer'
                WHEN 5 THEN 'Bert Synthetic Actor'
                WHEN 6 THEN 'Alice Synthetic Designer'
                WHEN 7 THEN 'Zoe Synthetic Actor'
                WHEN 8 THEN 'Xavier Synthetic Actor'
                ELSE (ARRAY['Downey', 'Smith', 'Garcia', 'Brown', 'Wilson', 'Martin',
                            'Anderson', 'Taylor', 'Lee', 'Miller', 'Clark', 'Walker'])
                     [1 + floor(det_uniform(g, 101) * 12)::integer]
                     || ', ' || (ARRAY['Robert', 'Tim', 'Angela', 'Yoko', 'Bert', 'Alice',
                                      'Zoe', 'Xavier', 'Anna', 'David', 'Maria', 'James'])
                     [1 + floor(det_uniform(g, 102) * 12)::integer] || ' ' || g
            END,
            CASE WHEN g % 7 = 0 THEN 'I' || g ELSE NULL END,
            200000 + g,
            CASE
                WHEN g IN (2, 3, 6) THEN 'f'
                WHEN g IN (1, 4, 5, 7, 8) THEN 'm'
                WHEN floor(det_uniform(g, 102) * 12)::integer IN (2,3,5,6,8,10) THEN 'f'
                ELSE 'm'
            END,
            CASE g WHEN 1 THEN 'D0001' WHEN 2 THEN 'A0002' WHEN 5 THEN 'B0005'
                ELSE chr((65 + (g % 6))::integer) || lpad((g % 10000)::text, 4, '0') END,
            left(md5('name-nf-' || g), 5),
            left(md5('surname-' || g), 5),
            md5('person-' || g)
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=people,
        ),
        LoadTask(
            'aka_name',
            """
WITH generated_source (id, name, imdb_index, imdb_id, gender, name_pcode_cf, name_pcode_nf,
            surname_pcode, md5sum) AS (SELECT
            g AS id,
            CASE g
                WHEN 1 THEN 'Downey Synthetic Robert'
                WHEN 2 THEN 'Angela Synthetic Actress'
                WHEN 3 THEN 'Yoko Synthetic Voice'
                WHEN 4 THEN 'Tim Synthetic Writer'
                WHEN 5 THEN 'Bert Synthetic Actor'
                WHEN 6 THEN 'Alice Synthetic Designer'
                WHEN 7 THEN 'Zoe Synthetic Actor'
                WHEN 8 THEN 'Xavier Synthetic Actor'
                ELSE (ARRAY['Downey', 'Smith', 'Garcia', 'Brown', 'Wilson', 'Martin',
                            'Anderson', 'Taylor', 'Lee', 'Miller', 'Clark', 'Walker'])
                     [1 + floor(det_uniform(g, 101) * 12)::integer]
                     || ', ' || (ARRAY['Robert', 'Tim', 'Angela', 'Yoko', 'Bert', 'Alice',
                                      'Zoe', 'Xavier', 'Anna', 'David', 'Maria', 'James'])
                     [1 + floor(det_uniform(g, 102) * 12)::integer] || ' ' || g
            END,
            CASE WHEN g % 7 = 0 THEN 'I' || g ELSE NULL END,
            200000 + g,
            CASE
                WHEN g IN (2, 3, 6) THEN 'f'
                WHEN g IN (1, 4, 5, 7, 8) THEN 'm'
                WHEN floor(det_uniform(g, 102) * 12)::integer IN (2,3,5,6,8,10) THEN 'f'
                ELSE 'm'
            END,
            CASE g WHEN 1 THEN 'D0001' WHEN 2 THEN 'A0002' WHEN 5 THEN 'B0005'
                ELSE chr((65 + (g % 6))::integer) || lpad((g % 10000)::text, 4, '0') END,
            left(md5('name-nf-' || g), 5),
            left(md5('surname-' || g), 5),
            md5('person-' || g)
        FROM generate_series($1::bigint, $2::bigint) AS g)
INSERT INTO aka_name
            (id, person_id, name, imdb_index, name_pcode_cf, name_pcode_nf,
             surname_pcode, md5sum)
        SELECT
            source.id,
            source.id,
            'Alias ' || source.name,
            'A' || source.id,
            left(md5('aka-cf-' || source.id), 5),
            left(md5('aka-nf-' || source.id), 5),
            left(md5('aka-surname-' || source.id), 5),
            md5('aka-' || source.id)
        FROM generated_source AS source;
        """,
            count=people,
        ),
        LoadTask(
            'char_name',
            """
INSERT INTO char_name
            (id, name, imdb_index, imdb_id, name_pcode_nf, surname_pcode, md5sum)
        SELECT
            g,
            CASE g
                WHEN 1 THEN 'Tony Stark Synthetic Hero'
                WHEN 2 THEN 'Sherlock Synthetic Hero'
                WHEN 3 THEN 'Kung Fu Panda Synthetic Hero'
                WHEN 4 THEN 'Iron Man Synthetic Hero'
                WHEN 5 THEN 'Queen'
                ELSE 'Synthetic Character ' || g
            END,
            'C' || g,
            300000 + g,
            left(md5('character-nf-' || g), 5),
            left(md5('character-surname-' || g), 5),
            md5('character-' || g)
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=characters,
        ),
        LoadTask(
            'keyword',
            """
INSERT INTO keyword (id, keyword, phonetic_code)
        SELECT
            g,
            CASE g
                WHEN 1 THEN 'character-name-in-title'
                WHEN 2 THEN 'sequel'
                WHEN 3 THEN 'marvel-cinematic-universe'
                WHEN 4 THEN 'superhero'
                WHEN 5 THEN 'second-part'
                WHEN 6 THEN 'marvel-comics'
                WHEN 7 THEN 'based-on-comic'
                WHEN 8 THEN 'tv-special'
                WHEN 9 THEN 'fight'
                WHEN 10 THEN 'violence'
                WHEN 11 THEN 'revenge'
                WHEN 12 THEN 'based-on-novel'
                WHEN 13 THEN 'murder'
                WHEN 14 THEN 'murder-in-title'
                WHEN 15 THEN 'blood'
                WHEN 16 THEN 'gore'
                WHEN 17 THEN 'death'
                WHEN 18 THEN 'female-nudity'
                WHEN 19 THEN 'hospital'
                WHEN 20 THEN 'computer-animation'
                WHEN 21 THEN 'computer-animated-movie'
                WHEN 22 THEN 'hand-to-hand-combat'
                WHEN 23 THEN 'hero'
                WHEN 24 THEN 'alienation'
                WHEN 25 THEN 'dignity'
                WHEN 26 THEN 'loner'
                WHEN 27 THEN 'nerd'
                WHEN 28 THEN '10,000-mile-club'
                WHEN 29 THEN 'claw'
                WHEN 30 THEN 'laser'
                WHEN 31 THEN 'magnet'
                WHEN 32 THEN 'web'
                ELSE 'keyword-' || g || '-topic-' || (g % 200)
            END,
            left(md5('keyword-' || g), 5)
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=keywords,
        ),
        LoadTask(
            'title',
            """
INSERT INTO title
            (id, title, imdb_index, kind_id, production_year, imdb_id, phonetic_code,
             episode_of_id, season_nr, episode_nr, series_years, md5sum)
        SELECT
            g,
            CASE g
                WHEN 1 THEN 'Synthetic Hero Movie'
                WHEN 2 THEN 'One Piece Synthetic Feature'
                WHEN 3 THEN 'Dragon Ball Z Synthetic Feature'
                WHEN 4 THEN 'Birdemic Synthetic Movie'
                WHEN 5 THEN 'Champion Synthetic Movie'
                WHEN 6 THEN 'Loser Synthetic Movie'
                WHEN 7 THEN 'Murder Synthetic Movie'
                WHEN 8 THEN 'YouTube Synthetic Movie'
                WHEN 9 THEN 'Kung Fu Panda Synthetic Feature'
                WHEN 10 THEN 'Iron Man Synthetic Feature'
                WHEN 11 THEN 'Sherlock Synthetic Feature'
                WHEN 12 THEN 'Saw Synthetic Horror'
                WHEN 13 THEN 'Freddy Synthetic Horror'
                WHEN 14 THEN 'Jason Synthetic Horror'
                WHEN 15 THEN 'Vampire Synthetic Horror'
                WHEN 16 THEN 'Shrek 2'
                WHEN 17 THEN 'Synthetic TV Series First'
                WHEN 18 THEN 'Synthetic TV Series Second'
                WHEN 19 THEN 'Synthetic Biography'
                WHEN 20 THEN 'Synthetic VHS Movie'
                WHEN 21 THEN 'Money Synthetic Film'
                WHEN 22 THEN 'Kung Fu Panda Legacy'
                ELSE CASE WHEN g % 100 = 7 THEN 'Murder Story '
                          WHEN g % 100 = 21 THEN 'Money Story '
                          ELSE 'Synthetic Title ' END || g
            END,
            CASE WHEN g % 9 = 0 THEN 'T' || g ELSE NULL END,
            CASE WHEN g IN (17, 18) THEN 2 WHEN g <= 22 THEN 1
                 WHEN g % 20 = 3 THEN 2 WHEN g % 20 IN (4, 5) THEN 3
                 WHEN g % 20 = 6 THEN 4 WHEN g % 20 = 7 THEN 5
                 WHEN g % 20 = 8 THEN 6 WHEN g % 20 = 9 THEN 7 ELSE 1 END,
            CASE g
                WHEN 1 THEN 2016 WHEN 2 THEN 2007 WHEN 3 THEN 2006 WHEN 4 THEN 2009
                WHEN 5 THEN 2008 WHEN 6 THEN 2007 WHEN 7 THEN 2016 WHEN 8 THEN 2007
                WHEN 9 THEN 2011 WHEN 10 THEN 2015 WHEN 11 THEN 2012 WHEN 12 THEN 2007
                WHEN 13 THEN 2008 WHEN 14 THEN 2009 WHEN 15 THEN 2015 WHEN 16 THEN 2004
                WHEN 17 THEN 2006 WHEN 18 THEN 2007 WHEN 19 THEN 1982 WHEN 20 THEN 1994
                WHEN 21 THEN 1998 WHEN 22 THEN 2008
                ELSE CASE WHEN g % 20 IN (4, 5)
                    THEN LEAST(2022, 1950 + (((g / 20) * 20 + 3) * 17) % 73 + g % 20 - 4)
                    ELSE 1950 + (g * 17) % 73 END
            END,
            400000 + g,
            left(md5('title-' || g), 5),
            CASE WHEN g > 22 AND g % 20 IN (4, 5) THEN (g / 20) * 20 + 3 ELSE NULL END,
            CASE WHEN g > 22 AND g % 20 IN (4, 5) THEN 1 ELSE NULL END,
            CASE WHEN g > 22 AND g % 20 IN (4, 5)
                 THEN 1 + ((g / 20) * 2 + g % 20 - 4) % 99 ELSE NULL END,
            NULL,
            md5('title-' || g)
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=titles,
        ),
        LoadTask(
            'aka_title',
            """
INSERT INTO aka_title
    (id, movie_id, title, imdb_index, kind_id, production_year, phonetic_code,
     episode_of_id, season_nr, episode_nr, note, md5sum)
SELECT id, id, 'Alternative ' || title, imdb_index, kind_id, production_year,
       phonetic_code, episode_of_id, season_nr, episode_nr, '(internet)', md5('aka-title-' || id)
FROM title WHERE id BETWEEN $1::bigint AND $2::bigint;
            """,
            count=titles,
            depends_on=('title',),
        ),
        LoadTask(
            'cast_info',
            f"""
INSERT INTO cast_info
            (id, person_id, movie_id, person_role_id, note, nr_order, role_id)
        SELECT
            g,
            1 + floor(power(det_uniform(g, 11), 1.5) * {people})::bigint,
            1 + ((g::bigint * {cast_stride} - 1) % {titles}),
            CASE WHEN credit.role_id IN (1, 2)
                 THEN 1 + floor(det_uniform(g, 12) * {characters})::bigint ELSE NULL END,
            CASE credit.role_id
                WHEN 3 THEN '(writer)' WHEN 4 THEN '(costume designer)'
                WHEN 5 THEN '(producer)' WHEN 6 THEN '(director)'
                ELSE (ARRAY['(voice)', '(uncredited)', NULL])
                     [1 + floor(det_uniform(g, 13) * 3)::bigint] END,
            1 + ((g - 1) / {titles})::bigint,
            credit.role_id
        FROM generate_series($1::bigint, $2::bigint) AS g
        CROSS JOIN LATERAL (
            SELECT CASE WHEN g <= {titles} THEN 1
                        WHEN g <= 2 * {titles} THEN 3
                        ELSE 1 + floor(det_uniform(g, 15) * 6)::bigint END AS role_id
        ) credit;
        """,
            count=cast_rows,
        ),
        LoadTask(
            'cast_info_anchors',
            f"""
WITH anchor_cast(person_id, person_role_id, note, role_id, nr_order) AS (
            VALUES
                (1, 1, '(actor)', 1, 1),
                (1, 1, '(voice) (uncredited)', 1, 2),
                (2, 5, '(voice)', 2, 3),
                (2, NULL, '(writer)', 3, 4),
                (3, 3, '(voice: English version)', 2, 5),
                (4, NULL, '(producer)', 5, 6),
                (4, NULL, '(writer)', 3, 7),
                (5, 1, '(uncredited)', 1, 8),
                (6, NULL, '(costume designer)', 4, 9),
                (7, 1, '(actor)', 1, 10),
                (8, 1, '(actor)', 1, 11)
        )
        INSERT INTO cast_info
            (id, person_id, movie_id, person_role_id, note, nr_order, role_id)
        SELECT
            {cast_rows} + (movie_id - 1) * 11 + nr_order,
            person_id,
            movie_id,
            person_role_id,
            note,
            nr_order,
            role_id
        FROM generate_series(1, 22) AS movie_id
        CROSS JOIN anchor_cast;
        """,
        ),
        LoadTask(
            'movie_keyword',
            f"""
INSERT INTO movie_keyword (id, movie_id, keyword_id)
        SELECT
            g,
            1 + ((g::bigint * {cast_stride} - 1) % {titles}),
            CASE WHEN g <= {titles} AND (1 + ((g::bigint * {cast_stride} - 1)
                           % {titles})) % 100 = 7 THEN 13
                 WHEN g <= {titles} AND (1 + ((g::bigint * {cast_stride} - 1)
                           % {titles})) % 100 = 21 THEN 2
                 ELSE 1 + floor(power(det_uniform(g, 21), 2.0) * {keywords})::bigint END
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=keyword_rows,
        ),
        LoadTask(
            'movie_keyword_anchors',
            f"""
INSERT INTO movie_keyword (id, movie_id, keyword_id)
        SELECT
            {keyword_rows} + (movie_id - 1) * 32 + keyword_id,
            movie_id,
            keyword_id
        FROM generate_series(1, 22) AS movie_id
        CROSS JOIN generate_series(1, 32) AS keyword_id;
        """,
        ),
        LoadTask(
            'movie_companies',
            f"""
INSERT INTO movie_companies
            (id, movie_id, company_id, company_type_id, note)
        SELECT
            g,
            1 + ((g::bigint * {company_stride} - 1) % {titles}),
            1 + floor(power(det_uniform(g, 31), 1.8) * {companies})::bigint,
            CASE WHEN g <= {titles} THEN 1
                 ELSE 2 + floor(det_uniform(g, 32) * 3)::bigint END,
            event.label || ' (' || release.year::text || ')'
        FROM generate_series($1::bigint, $2::bigint) AS g
        JOIN title t ON t.id=1 + ((g::bigint * {company_stride} - 1) % {titles})
        CROSS JOIN LATERAL (
            SELECT CASE
                WHEN t.production_year BETWEEN 1990 AND 1994
                  OR (t.production_year BETWEEN 1980 AND 1999 AND det_uniform(g, 33)<0.25)
                    THEN '(VHS) (USA)'
                ELSE (ARRAY['(co-production) (presents)', '(worldwide)', '(Blu-ray) (USA)',
                            '(theatrical) (France)'])
                     [1 + floor(det_uniform(g, 33) * 4)::integer]
            END AS label
        ) event
        CROSS JOIN LATERAL (
            SELECT CASE
                WHEN event.label='(VHS) (USA)' THEN
                    CASE WHEN t.production_year BETWEEN 1990 AND 1994 THEN 1994
                         ELSE t.production_year
                              + floor(det_uniform(g, 34)*(2006-t.production_year))::integer END
                WHEN event.label='(Blu-ray) (USA)' THEN
                    GREATEST(2006,t.production_year)
                    + floor(det_uniform(g, 34)*(2025-GREATEST(2006,t.production_year)))::integer
                ELSE LEAST(2024,t.production_year+floor(det_uniform(g, 34)*3)::integer)
            END AS year
        ) release;
        """,
            count=company_rows,
            depends_on=('title',),
        ),
        LoadTask(
            'movie_companies_anchors',
            f"""
INSERT INTO movie_companies
            (id, movie_id, company_id, company_type_id, note)
        SELECT
            {company_rows} + (movie_id - 1) * 26 + (company_id - 1) * 2 + company_type_id,
            movie_id,
            company_id,
            company_type_id,
            CASE
                WHEN company_id=13 THEN NULL
                WHEN company_id=6 THEN '(theatrical) (Japan) ('
                    || GREATEST(2007,t.production_year)::text || ')'
                WHEN company_id IN (3,11) THEN '(co-production) (presents) '
                    || 'Synthetic Warner Film Money (' || t.production_year::text || ')'
                WHEN company_type_id=1 AND t.production_year<=1994 THEN '(VHS) (USA) (1994)'
                WHEN company_type_id=2 THEN '(Blu-ray) (USA) ('
                    || GREATEST(2006,t.production_year)::text || ')'
                ELSE '(theatrical) (France) (' || t.production_year::text || ')'
            END
        FROM generate_series(1, 22) AS movie_id
        JOIN title t ON t.id=movie_id
        CROSS JOIN generate_series(1, 13) AS company_id
        CROSS JOIN generate_series(1, 2) AS company_type_id;
        """,
            depends_on=('title',),
        ),
        LoadTask(
            'movie_info',
            f"""
INSERT INTO movie_info (id, movie_id, info_type_id, info, note)
        SELECT
            g,
            1 + ((g::bigint * {info_stride} - 1) % {titles}),
            (ARRAY[4, 6, 7, 8])[s.slot],
            CASE s.slot
                WHEN 1 THEN (ARRAY['Horror', 'Thriller', 'Action', 'Sci-Fi'])[s.pick]
                WHEN 2 THEN '$' || (100000 + (g::bigint * 7919) % 200000000)
                WHEN 3 THEN (ARRAY['USA', 'Germany', 'Sweden', 'Norway'])[s.pick]
                ELSE 'USA: ' || LEAST(2024, t.production_year + g % 3)::text
            END,
            CASE WHEN s.slot = 4 THEN '(internet)' ELSE NULL END
        FROM generate_series($1::bigint, $2::bigint) AS g
        JOIN title t ON t.id = 1 + ((g::bigint * {info_stride} - 1) % {titles})
        CROSS JOIN LATERAL (
            SELECT 1 + ((g - 1) / {titles})::bigint AS slot,
                   1 + floor(det_uniform(g, 42) * 4)::bigint AS pick
        ) AS s;
        """,
            count=info_rows,
            depends_on=('title',),
        ),
        LoadTask(
            'movie_info_anchors',
            f"""
WITH anchor_info(info_type_id, info, note) AS (
            VALUES
                (4, 'Horror', NULL),
                (4, 'Thriller', NULL),
                (4, 'Action', NULL),
                (4, 'Sci-Fi', NULL),
                (4, 'Crime', NULL),
                (4, 'War', NULL),
                (4, 'Drama', NULL),
                (4, 'Family', NULL),
                (4, 'Western', NULL),
                (7, 'Sweden', NULL),
                (7, 'Norway', NULL),
                (7, 'Germany', NULL),
                (7, 'Denmark', NULL),
                (7, 'USA', NULL),
                (7, 'Bulgaria', NULL),
                (8, 'USA: 1994', '(internet)'),
                (8, 'USA: 2007', '(internet)'),
                (8, 'USA: 2008', '(internet)'),
                (8, 'USA: 2011', '(internet)'),
                (8, 'Japan:2007', '(internet)'),
                (8, 'Japan:2011', '(internet)'),
                (10, 'Synthetic trivia', NULL),
                (11, '180 cm', NULL)
        )
        INSERT INTO movie_info (id, movie_id, info_type_id, info, note)
        SELECT
            ({info_rows} + row_number() OVER (
                ORDER BY movie_id, info_type_id, info, note
            ))::bigint,
            movie_id,
            info_type_id,
            info,
            note
        FROM generate_series(1, 22) AS movie_id
        CROSS JOIN anchor_info;
        """,
        ),
        LoadTask(
            'movie_info_idx',
            f"""
INSERT INTO movie_info_idx (id, movie_id, info_type_id, info, note)
SELECT g, 1 + (g - 1) % {titles},
       CASE WHEN g <= {titles} THEN 3 ELSE 5 END,
       CASE WHEN g <= {titles}
            THEN CASE WHEN g=7 THEN '7.4'
                      ELSE to_char(2.0 + det_uniform(g, 1) * 8.0, 'FM99.0') END
            ELSE (1000 + ((1 + (g - 1) % {titles}) * 15485863) % 500000)::text END,
       NULL
FROM generate_series($1::bigint, $2::bigint) AS g;
            """,
            count=info_index_rows,
        ),
        LoadTask(
            'movie_info_idx_ranks',
            f"""
WITH ranked AS (
    SELECT r.movie_id,
           row_number() OVER (ORDER BY r.info::numeric DESC, v.info::numeric DESC,
                r.movie_id) AS top_rank,
           row_number() OVER (ORDER BY r.info::numeric ASC, v.info::numeric ASC,
                r.movie_id DESC) AS bottom_rank
    FROM movie_info_idx r JOIN movie_info_idx v USING (movie_id)
    JOIN title t ON t.id = r.movie_id
    WHERE r.info_type_id = 3 AND v.info_type_id = 5 AND t.kind_id = 1
)
INSERT INTO movie_info_idx (id, movie_id, info_type_id, info, note)
SELECT {info_index_rows} + top_rank, movie_id, 1, top_rank::text, NULL
FROM ranked WHERE top_rank <= 250
UNION ALL
SELECT {info_index_rows} + 250 + bottom_rank, movie_id, 2, bottom_rank::text, NULL
FROM ranked WHERE bottom_rank <= 10;
            """,
            depends_on=('movie_info_idx', 'title'),
        ),
        LoadTask(
            'person_info',
            """
INSERT INTO person_info (id, person_id, info_type_id, info, note)
        SELECT
            g,
            g,
            CASE WHEN g % 3 = 0 THEN 9 WHEN g % 3 = 1 THEN 10 ELSE 11 END,
            CASE WHEN g % 3 = 0 THEN 'Synthetic biography'
                 WHEN g % 3 = 1 THEN 'Synthetic trivia' ELSE '180 cm' END,
            CASE WHEN g % 3 = 0 THEN 'Volker Boehm' ELSE NULL END
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=people,
        ),
        LoadTask(
            'person_info_anchors',
            f"""
WITH anchor_person_info(info_type_id, info, note) AS (
            VALUES
                (9, 'Synthetic mini biography', 'Volker Boehm'),
                (10, 'Queen', 'Synthetic trivia'),
                (11, '180 cm', NULL)
        )
        INSERT INTO person_info (id, person_id, info_type_id, info, note)
        SELECT
            ({people} + row_number() OVER (ORDER BY person_id, info_type_id, info, note))::bigint,
            person_id,
            info_type_id,
            info,
            note
        FROM generate_series(1, 6) AS person_id
        CROSS JOIN anchor_person_info;
        """,
        ),
        LoadTask(
            'complete_cast',
            f"""
INSERT INTO complete_cast (id, movie_id, subject_id, status_id)
        SELECT g, 1 + (g - 1) % {titles}, 1 + ((g - 1) / {titles})::bigint,
               CASE WHEN g % 3 = 0 THEN 3 ELSE 4 END
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=titles * 2,
        ),
        LoadTask(
            'movie_link',
            f"""
INSERT INTO movie_link (id, movie_id, linked_movie_id, link_type_id)
        SELECT
            g,
            1 + (g - 1) % {titles},
            1 + ((g + floor(det_uniform(g, 61) * ({titles} - 1))::bigint) % {titles}),
            1 + g % 7
        FROM generate_series($1::bigint, $2::bigint) AS g;
        """,
            count=movie_links,
        ),
        LoadTask(
            'movie_link_anchors',
            f"""
INSERT INTO movie_link (id, movie_id, linked_movie_id, link_type_id)
        SELECT
            {movie_links} + (movie_id - 1) * 7 + link_type_id,
            movie_id,
            CASE WHEN movie_id=21 AND link_type_id IN (1,2,4,6) THEN 19
                 WHEN movie_id = 17 THEN 18 WHEN movie_id = 18 THEN 17 ELSE movie_id + 1 END,
            link_type_id
        FROM generate_series(1, 22) AS movie_id
        CROSS JOIN generate_series(1, 7) AS link_type_id;
        """,
        ),
    )
    root = Path(__file__).parent
    return LoadPlan(
        schemas=('imdb',),
        schema_sql=(root / 'sql/initialization-schema.sql').read_text(),
        data=data,
        indexes=read_sql_tasks(root / 'initialization-indexes.json'),
        constraints=read_sql_tasks(root / 'initialization-constraints.json'),
        prepare_sql="""CREATE FUNCTION imdb.det_uniform(seed bigint, stream integer)
        RETURNS double precision
        LANGUAGE sql IMMUTABLE
        AS $$
            SELECT (hashint8($1 * 1000003 + $2 * 7919) & 2147483647)::double precision
                   / 2147483648.0
        $$;""",
        after_data_sql="""
-- Anchors are compatibility fixtures, but must obey the same temporal invariants.
UPDATE imdb.cast_info ci
SET role_id = CASE WHEN n.gender='f' THEN 2 ELSE 1 END
FROM imdb.name n WHERE ci.person_id=n.id AND ci.role_id IN (1,2);
UPDATE imdb.movie_info mi
SET info = regexp_replace(mi.info, '[0-9]{4}', t.production_year::text)
FROM imdb.title t
WHERE t.id = mi.movie_id AND mi.info_type_id = 8
  AND substring(mi.info FROM '[0-9]{4}')::integer < t.production_year;
-- A relation is a set, not extra copies of a keyword or an identical attribute.
WITH duplicates AS (
    SELECT id, row_number() OVER (PARTITION BY movie_id, keyword_id ORDER BY id) AS rn
    FROM imdb.movie_keyword
)
DELETE FROM imdb.movie_keyword t USING duplicates d WHERE t.id=d.id AND d.rn>1;
WITH duplicates AS (
    SELECT id, row_number() OVER (PARTITION BY movie_id, info_type_id, info ORDER BY id) AS rn
    FROM imdb.movie_info
)
DELETE FROM imdb.movie_info t USING duplicates d WHERE t.id=d.id AND d.rn>1;
-- A sequel/follower/feature/reference points to an earlier title; inverse links point forward.
UPDATE imdb.movie_link ml SET movie_id=ml.linked_movie_id, linked_movie_id=ml.movie_id
FROM imdb.title a, imdb.title b
WHERE a.id=ml.movie_id AND b.id=ml.linked_movie_id
  AND ((ml.link_type_id IN (1,2,4,6) AND (a.production_year,a.id)<(b.production_year,b.id))
    OR (ml.link_type_id IN (3,5,7) AND (a.production_year,a.id)>(b.production_year,b.id)));
WITH duplicates AS (
    SELECT id, row_number() OVER (PARTITION BY movie_id, linked_movie_id, link_type_id
                                  ORDER BY id) AS rn
    FROM imdb.movie_link
)
DELETE FROM imdb.movie_link t USING duplicates d WHERE t.id=d.id AND d.rn>1;
DROP FUNCTION imdb.det_uniform(bigint, integer);""",
        finalize_sql=(root / 'sql/initialization-finalize.sql').read_text(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Generate synthetic imdb data in bounded batches')
    parser.add_argument('--scale', type=float, required=True)
    parser.add_argument('--batch-rows', type=int, default=100_000)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    try:
        plan = build_load_plan(args.scale)
        if args.batch_rows < 1 or args.workers < 1:
            raise ValueError('batch-rows and workers must be positive')
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO)

    async def generate():
        import asyncpg

        db_conf = dict(
            host=os.environ.get('PGHOST', 'localhost'),
            port=int(os.environ.get('PGPORT', '5432')),
            user=os.environ.get('PGUSER', 'postgres'),
            password=os.environ.get('PGPASSWORD'),
            database=os.environ['PGDATABASE'],
        )
        db = await asyncpg.connect(**db_conf)
        try:
            await db.execute(plan.prepare_sql)
            await run_tasks(
                logging.getLogger(__name__),
                plan.data,
                db_conf,
                LoadOptions(
                    workers=args.workers, batch_rows=args.batch_rows, synchronous_commit='keep'
                ),
                phase='data',
                search_path='imdb, public',
            )
            await db.execute(plan.after_data_sql)
        finally:
            await db.close()

    asyncio.run(generate())


if __name__ == '__main__':
    main()
