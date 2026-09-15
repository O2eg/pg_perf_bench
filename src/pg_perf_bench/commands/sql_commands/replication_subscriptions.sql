-- Subscription connection strings contain credentials. Read only named non-secret
-- columns, without requiring access to subconninfo.
SELECT
    s.subname, s.subenabled, pg_catalog.pg_get_userbyid(s.subowner)::text AS owner,
    s.subslotname, s.subsynccommit AS worker_synchronous_commit,
    s.subpublications::text AS publications,
    CASE WHEN pg_catalog.pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')
         THEN count(w.pid) END AS running_workers
FROM pg_catalog.pg_subscription s
LEFT JOIN pg_catalog.pg_stat_subscription w ON w.subid = s.oid
WHERE s.subdbid = (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database())
GROUP BY s.oid, s.subname, s.subenabled, s.subowner, s.subslotname,
         s.subsynccommit, s.subpublications
ORDER BY s.subname;
