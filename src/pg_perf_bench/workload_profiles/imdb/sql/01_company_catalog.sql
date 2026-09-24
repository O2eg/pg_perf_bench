SET search_path=imdb;

SELECT
    cn.name AS company,
    t.production_year,
    count(DISTINCT t.id) AS titles,
    round(avg(CAST(mi_idx.info AS numeric)), 2) AS avg_rating
FROM company_name AS cn
JOIN (SELECT DISTINCT company_id, movie_id FROM movie_companies) AS mc ON mc.company_id = cn.id
JOIN title AS t ON t.id = mc.movie_id
JOIN movie_info_idx AS mi_idx ON mi_idx.movie_id = t.id
JOIN info_type AS it ON it.id = mi_idx.info_type_id AND it.info = 'rating'
WHERE mi_idx.info_type_id=3 AND t.production_year BETWEEN 2000 AND 2022
  AND CAST(mi_idx.info AS numeric) >= 6.0
GROUP BY cn.id, cn.name, t.production_year
ORDER BY titles DESC, avg_rating DESC, cn.id, t.production_year
LIMIT 50;
