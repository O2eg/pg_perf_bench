SET search_path=imdb;

SELECT MIN(n.name) AS voicing_actress,MIN(t.title) AS jap_engl_voiced_movie
FROM title t JOIN cast_info ci ON ci.movie_id=t.id
JOIN name n ON n.id=ci.person_id
WHERE t.production_year>2000 AND n.gender='f' AND ci.role_id=2
AND ci.note IN ('(voice)','(voice: Japanese version)','(voice) (uncredited)',
                '(voice: English version)')
AND EXISTS (SELECT FROM char_name chn WHERE chn.id=ci.person_role_id)
AND EXISTS (SELECT FROM aka_name an WHERE an.person_id=n.id)
AND EXISTS (SELECT FROM movie_companies mc JOIN company_name cn ON cn.id=mc.company_id
            WHERE mc.movie_id=t.id AND cn.country_code='[us]')
AND EXISTS (SELECT FROM movie_info mi WHERE mi.movie_id=t.id AND mi.info_type_id=8);
