SET search_path=imdb;

-- The generator supplies a nonempty, dense ID range. Fetch its actual bounds.
SELECT min(id) AS lo, max(id) AS hi FROM title
\gset bounds_
\set movie_id random(:bounds_lo, :bounds_hi)
-- One movie card; independent child collections cannot multiply one another.
WITH picked AS (
 SELECT :movie_id::bigint AS id
)
SELECT t.id,t.title,t.production_year,r.info::numeric AS rating,
       b.info AS budget,
       (SELECT jsonb_agg(jsonb_build_object('id',n.id,'name',n.name,'role',rt.role)
                         ORDER BY ci.nr_order,ci.id)
        FROM cast_info ci JOIN name n ON n.id=ci.person_id
        JOIN role_type rt ON rt.id=ci.role_id WHERE ci.movie_id=t.id) AS credits,
       (SELECT jsonb_agg(k.keyword ORDER BY k.id) FROM movie_keyword mk
        JOIN keyword k ON k.id=mk.keyword_id WHERE mk.movie_id=t.id) AS keywords,
       (SELECT jsonb_agg(x.name ORDER BY x.id) FROM (
          SELECT DISTINCT cn.id,cn.name FROM movie_companies mc
          JOIN company_name cn ON cn.id=mc.company_id WHERE mc.movie_id=t.id
        ) x) AS companies
FROM picked p JOIN title t ON t.id=p.id
JOIN movie_info_idx r ON r.movie_id=t.id AND r.info_type_id=3
JOIN movie_info b ON b.movie_id=t.id AND b.info_type_id=6;
