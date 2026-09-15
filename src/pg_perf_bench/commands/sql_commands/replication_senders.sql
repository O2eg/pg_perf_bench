-- Adapted from pg_diag replication/physical_replication*.sql (PG 10+).
-- A cascading standby may forward received WAL before it is replayed.
WITH local_position AS (
    SELECT CASE WHEN pg_catalog.pg_is_in_recovery()
        THEN greatest(pg_catalog.pg_last_wal_receive_lsn(), pg_catalog.pg_last_wal_replay_lsn())
        ELSE pg_catalog.pg_current_wal_lsn() END AS lsn
)
SELECT
    r.pid,
    coalesce(s.slot_type, 'physical') AS sender_kind,
    s.slot_name,
    r.usename,
    r.application_name,
    r.client_addr::text AS client_addr,
    r.state,
    r.sync_state,
    r.sync_priority,
    r.backend_xmin::text AS backend_xmin,
    r.sent_lsn::text AS sent_lsn,
    r.write_lsn::text AS write_lsn,
    r.flush_lsn::text AS flush_lsn,
    r.replay_lsn::text AS replay_lsn,
    pg_catalog.pg_wal_lsn_diff(p.lsn, r.sent_lsn)::bigint AS unsent_wal_bytes,
    pg_catalog.pg_wal_lsn_diff(p.lsn, r.flush_lsn)::bigint AS unflushed_wal_bytes,
    pg_catalog.pg_wal_lsn_diff(p.lsn, r.replay_lsn)::bigint AS unreplayed_wal_bytes,
    extract(epoch FROM r.write_lag)::double precision AS write_lag_seconds,
    extract(epoch FROM r.flush_lag)::double precision AS flush_lag_seconds,
    extract(epoch FROM r.replay_lag)::double precision AS replay_lag_seconds,
    to_jsonb(r)->>'reply_time' AS reply_time
FROM pg_catalog.pg_stat_replication r
LEFT JOIN pg_catalog.pg_replication_slots s ON s.active_pid = r.pid
CROSS JOIN local_position p
ORDER BY r.application_name, r.pid;
