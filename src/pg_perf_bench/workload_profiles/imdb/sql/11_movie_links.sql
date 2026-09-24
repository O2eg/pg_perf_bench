SET search_path=imdb;

-- A coherent source/target pair, with the direction of the relation preserved.
SELECT t.id,t.title,t.production_year,lt.link,
       previous.id AS related_id,previous.title AS related_title,
       previous.production_year AS related_year
FROM title t JOIN movie_link ml ON ml.movie_id=t.id
JOIN link_type lt ON lt.id=ml.link_type_id
JOIN title previous ON previous.id=ml.linked_movie_id
WHERE t.title LIKE 'Money%' AND ml.link_type_id IN (1,2)
ORDER BY t.id,ml.link_type_id,previous.id LIMIT 100;
