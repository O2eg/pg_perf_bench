SET search_path=imdb;

SELECT MIN(mi.info) AS movie_genre,MIN(v.info::numeric) AS movie_rating,
       MIN(t.title) AS movie_title
FROM title t JOIN movie_info mi ON mi.movie_id=t.id AND mi.info_type_id=4
JOIN movie_info_idx v ON v.movie_id=t.id AND v.info_type_id=3
WHERE t.production_year BETWEEN 2008 AND 2014 AND mi.info IN ('Horror','Thriller')
AND mi.note IS NULL AND v.info::numeric>8
AND EXISTS (SELECT FROM cast_info ci JOIN name n ON n.id=ci.person_id
            WHERE ci.movie_id=t.id AND n.gender='f'
              AND ci.note IN ('(writer)','(head writer)','(written by)','(story)','(story editor)'));
