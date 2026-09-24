/* Disabled pending performance optimization.
Excluded from the default pgbench workload; preserved for revision.

SET search_path=imdb;

\set genre_no random(1, 4)
-- Role coverage within one genre and a defined reporting period.
WITH genre_titles AS (
 SELECT t.id FROM movie_info mi JOIN title t ON t.id=mi.movie_id
 WHERE mi.info_type_id=4
   AND mi.info=(ARRAY['Horror','Thriller','Action','Sci-Fi'])[:genre_no]
   AND t.production_year BETWEEN 2010 AND 2022
 OFFSET 0
)
SELECT rt.role,count(*) AS credits,count(DISTINCT ci.person_id) AS people,
       count(DISTINCT ci.movie_id) AS titles
FROM genre_titles t JOIN cast_info ci ON ci.movie_id=t.id
JOIN role_type rt ON rt.id=ci.role_id
GROUP BY rt.id,rt.role ORDER BY credits DESC,rt.id;
*/
