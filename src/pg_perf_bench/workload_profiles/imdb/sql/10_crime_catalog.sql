/* Disabled pending performance optimization.
Excluded from the default pgbench workload; preserved for revision.

SET search_path=imdb;

-- Highly rated modern mystery titles. Prefix lookup includes scaled background titles.
WITH titles AS (
 SELECT id,title,production_year FROM title
 WHERE title LIKE 'Murder%' AND production_year BETWEEN 2010 AND 2022
 OFFSET 0
)
SELECT t.id,t.title,t.production_year,r.info::numeric AS rating
FROM titles t JOIN movie_info_idx r ON r.movie_id=t.id AND r.info_type_id=3
WHERE r.info::numeric>=6
AND EXISTS (SELECT FROM movie_keyword mk WHERE mk.movie_id=t.id AND mk.keyword_id IN (13,14))
AND EXISTS (SELECT FROM movie_info mi WHERE mi.movie_id=t.id AND mi.info_type_id=7
            AND mi.info IN ('USA','Sweden','Norway','Germany','Denmark'))
ORDER BY r.info::numeric DESC,t.id LIMIT 50;
*/
