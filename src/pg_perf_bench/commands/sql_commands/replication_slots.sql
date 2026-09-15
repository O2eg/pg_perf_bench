-- Adapted from pg_diag replication/replication_slots_pg*.sql.
-- Inspect optional view fields through JSON for compatibility with PG 10-18.
WITH local_position AS (
    SELECT CASE WHEN pg_catalog.pg_is_in_recovery()
        THEN greatest(pg_catalog.pg_last_wal_receive_lsn(), pg_catalog.pg_last_wal_replay_lsn())
        ELSE pg_catalog.pg_current_wal_lsn() END AS lsn
)
SELECT
    s.slot_name, s.slot_type, s.database, s.plugin, s.temporary, s.active, s.active_pid,
    s.xmin::text AS xmin,
    s.catalog_xmin::text AS catalog_xmin,
    s.restart_lsn::text AS restart_lsn,
    s.confirmed_flush_lsn::text AS confirmed_flush_lsn,
    pg_catalog.pg_wal_lsn_diff(p.lsn, s.restart_lsn)::bigint AS wal_distance_bytes,
    to_jsonb(s)->>'wal_status' AS wal_status,
    (to_jsonb(s)->>'safe_wal_size')::bigint AS safe_wal_size_bytes,
    to_jsonb(s)->>'inactive_since' AS inactive_since,
    to_jsonb(s)->>'invalidation_reason' AS invalidation_reason,
    (to_jsonb(s)->>'two_phase')::boolean AS two_phase,
    (to_jsonb(s)->>'failover')::boolean AS failover,
    (to_jsonb(s)->>'synced')::boolean AS synced,
    (to_jsonb(s)->>'conflicting')::boolean AS conflicting
FROM pg_catalog.pg_replication_slots s CROSS JOIN local_position p
ORDER BY s.slot_type, s.slot_name;
