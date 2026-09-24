SET search_path=imdb;

SELECT MIN(kt.kind) AS movie_kind,MIN(t.title) AS complete_us_internet_movie
FROM title t JOIN kind_type kt ON kt.id=t.kind_id
WHERE t.production_year>1990 AND kt.kind IN ('movie','tv movie','video movie','video game')
AND EXISTS (SELECT FROM complete_cast cc WHERE cc.movie_id=t.id AND cc.status_id=4)
AND EXISTS (SELECT FROM movie_info mi WHERE mi.movie_id=t.id AND mi.info_type_id=8
            AND mi.note LIKE '%internet%' AND mi.info IS NOT NULL
            AND (mi.info LIKE 'USA:% 199%' OR mi.info LIKE 'USA:% 200%'))
AND EXISTS (SELECT FROM movie_companies mc JOIN company_name cn ON cn.id=mc.company_id
            JOIN company_type ct ON ct.id=mc.company_type_id
            WHERE mc.movie_id=t.id AND cn.country_code='[us]')
AND EXISTS (SELECT FROM movie_keyword mk JOIN keyword k ON k.id=mk.keyword_id
            WHERE mk.movie_id=t.id);
