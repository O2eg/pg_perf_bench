SET search_path=imdb;

\set keyword_id random(1, 32)
-- Popular cast members for one theme in the contemporary catalog.
SELECT n.id AS person_id,n.name,k.keyword,count(DISTINCT t.id) AS title_count,
       min(t.production_year) AS first_year,max(t.production_year) AS last_year
FROM movie_keyword mk
JOIN keyword k ON k.id=mk.keyword_id
JOIN title t ON t.id=mk.movie_id
JOIN cast_info ci ON ci.movie_id=t.id AND ci.role_id IN (1,2)
JOIN name n ON n.id=ci.person_id
WHERE mk.keyword_id=:keyword_id AND t.production_year BETWEEN 2000 AND 2022
GROUP BY n.id,n.name,k.keyword
ORDER BY title_count DESC,last_year DESC,n.id LIMIT 100;
