SET search_path=imdb;

\set first_no random(1, 4)
-- Name search over both scaled data and compatibility fixtures, before credit lookups.
WITH people AS (
 SELECT id,name FROM name WHERE name LIKE
   (ARRAY['Downey%Robert%','%Tim%','%Angela%','%Alice%'])[:first_no]
 ORDER BY name,id LIMIT 50
)
SELECT n.id,n.name,count(DISTINCT ci.movie_id) AS titles,
       min(t.production_year) AS first_year,max(t.production_year) AS last_year
FROM people n JOIN cast_info ci ON ci.person_id=n.id JOIN title t ON t.id=ci.movie_id
GROUP BY n.id,n.name ORDER BY n.name,n.id;
