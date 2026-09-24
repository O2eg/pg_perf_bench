SET search_path=imdb;

\set genre_no random(1, 4)
-- Complete movie rows, ordered by popularity, with genre and writer credit requirements.
SELECT t.id,t.title,t.production_year,v.info::bigint AS votes,r.info::numeric AS rating
FROM movie_info_idx v JOIN title t ON t.id=v.movie_id
JOIN movie_info_idx r ON r.movie_id=t.id AND r.info_type_id=3
WHERE v.info_type_id=5 AND t.production_year BETWEEN 2010 AND 2022
AND EXISTS (SELECT FROM movie_info mi WHERE mi.movie_id=t.id AND mi.info_type_id=4
            AND mi.info=(ARRAY['Horror','Thriller','Action','Sci-Fi'])[:genre_no])
AND EXISTS (SELECT FROM cast_info ci WHERE ci.movie_id=t.id AND ci.role_id=3)
ORDER BY v.info::numeric DESC,v.movie_id LIMIT 20;
