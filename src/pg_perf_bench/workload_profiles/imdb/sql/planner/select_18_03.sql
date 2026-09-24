SET search_path=imdb;

-- Independent minima retained for the planner probe; existence does not multiply movie rows.
SELECT MIN(mi.info) AS movie_genre,MIN(v.info::numeric) AS movie_votes,
       MIN(t.title) AS movie_title
FROM title t
JOIN movie_info mi ON mi.movie_id=t.id AND mi.info_type_id=4
JOIN movie_info_idx v ON v.movie_id=t.id AND v.info_type_id=5
WHERE mi.info IN ('Horror','Action','Sci-Fi','Thriller','Crime','War')
AND EXISTS (SELECT FROM cast_info ci JOIN name n ON n.id=ci.person_id
            WHERE ci.movie_id=t.id AND n.gender='m'
              AND ci.note IN ('(writer)','(head writer)','(written by)','(story)','(story editor)'));
