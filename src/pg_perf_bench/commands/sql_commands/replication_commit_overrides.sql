-- Based on pg_diag synchronous_replication_status.sql; never expose unrelated settings.
SELECT
    CASE WHEN s.setdatabase = 0 THEN 'all databases' ELSE d.datname::text END AS database,
    CASE WHEN s.setrole = 0 THEN 'all roles' ELSE r.rolname::text END AS role,
    substr(c.entry, strpos(c.entry, '=') + 1) AS synchronous_commit,
    (s.setdatabase = 0 OR d.datname = current_database())
        AND (s.setrole = 0 OR r.rolname = current_user) AS applies_to_collector
FROM pg_catalog.pg_db_role_setting s
CROSS JOIN LATERAL unnest(s.setconfig) c(entry)
LEFT JOIN pg_catalog.pg_roles r ON r.oid = s.setrole
LEFT JOIN pg_catalog.pg_database d ON d.oid = s.setdatabase
WHERE split_part(c.entry, '=', 1) = 'synchronous_commit'
ORDER BY database, role;
