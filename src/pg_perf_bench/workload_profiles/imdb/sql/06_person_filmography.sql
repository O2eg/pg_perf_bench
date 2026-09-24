SET search_path=imdb;

-- The generator supplies a nonempty, dense ID range. Fetch its actual bounds.
SELECT min(id) AS lo, max(id) AS hi FROM cast_info
\gset bounds_
\set credit_id random(:bounds_lo, :bounds_hi)
-- Pick an existing credit, then show that participant's actual filmography.
WITH picked AS (
 SELECT person_id FROM cast_info
 WHERE id=:credit_id::bigint
)
SELECT t.id,t.title,t.production_year,n.id AS person_id,n.name,
       array_agg(DISTINCT rt.role ORDER BY rt.role) AS roles,r.info::numeric AS rating
FROM picked p JOIN name n ON n.id=p.person_id
JOIN cast_info ci ON ci.person_id=n.id JOIN title t ON t.id=ci.movie_id
JOIN role_type rt ON rt.id=ci.role_id
JOIN movie_info_idx r ON r.movie_id=t.id AND r.info_type_id=3
GROUP BY t.id,t.title,t.production_year,n.id,n.name,r.info
ORDER BY t.production_year DESC,t.id LIMIT 100;
