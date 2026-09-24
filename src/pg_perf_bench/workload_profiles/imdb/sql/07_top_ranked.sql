SET search_path=imdb;

SELECT rank.info::integer AS rank,t.id,t.title,t.production_year,
       r.info::numeric AS rating,v.info::bigint AS votes
FROM movie_info_idx rank JOIN title t ON t.id=rank.movie_id
JOIN movie_info_idx r ON r.movie_id=t.id AND r.info_type_id=3
JOIN movie_info_idx v ON v.movie_id=t.id AND v.info_type_id=5
WHERE rank.info_type_id=1 ORDER BY rank.info::numeric LIMIT 50;
