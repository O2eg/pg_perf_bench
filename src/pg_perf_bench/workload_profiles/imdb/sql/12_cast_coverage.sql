SET search_path=imdb;

-- Editorial catalog completeness by production year, counting each title once per status.
SELECT t.production_year,subject.kind AS subject,status.kind AS status,count(*) AS titles
FROM title t JOIN complete_cast cc ON cc.movie_id=t.id
JOIN comp_cast_type subject ON subject.id=cc.subject_id
JOIN comp_cast_type status ON status.id=cc.status_id
WHERE t.production_year BETWEEN 2010 AND 2022
GROUP BY t.production_year,subject.id,subject.kind,status.id,status.kind
ORDER BY t.production_year,subject.id,status.id;
