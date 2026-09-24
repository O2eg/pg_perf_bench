SET search_path=imdb;

-- US VHS releases in 1994, including earlier productions, in stable catalog order.
SELECT t.id,t.title,t.production_year,1994 AS vhs_release_year
FROM title t WHERE t.production_year BETWEEN 1990 AND 1994
AND EXISTS (SELECT FROM movie_companies mc WHERE mc.movie_id=t.id
            AND mc.company_type_id=1 AND mc.note LIKE '%(VHS)%'
            AND mc.note LIKE '%(USA)%' AND mc.note LIKE '%(1994)%')
AND EXISTS (SELECT FROM movie_info mi WHERE mi.movie_id=t.id
            AND mi.info_type_id=7 AND mi.info='USA')
ORDER BY t.id LIMIT 100;
