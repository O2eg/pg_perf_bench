-- Adapted from pg_diag replication/synchronous_replication_status.sql.
-- Keep configured policy separate from connected senders and session overrides.
WITH policy AS (
    SELECT
        pg_catalog.pg_is_in_recovery() AS in_recovery,
        btrim(current_setting('synchronous_standby_names')) AS sync_names,
        current_setting('synchronous_commit') AS sync_commit,
        pg_catalog.pg_has_role(current_user, 'pg_read_all_stats', 'USAGE') AS full_stats
), senders AS (
    SELECT count(*) AS total,
           count(*) FILTER (WHERE state = 'streaming') AS streaming,
           count(*) FILTER (WHERE sync_state = 'sync') AS sync,
           count(*) FILTER (WHERE sync_state = 'quorum') AS quorum
    FROM pg_catalog.pg_stat_replication
), slots AS (
    SELECT count(*) FILTER (WHERE slot_type = 'physical') AS physical,
           count(*) FILTER (WHERE slot_type = 'logical') AS logical,
           count(*) FILTER (WHERE NOT active) AS inactive
    FROM pg_catalog.pg_replication_slots
), waits AS (
    SELECT count(*) AS sync_rep_waiters
    FROM pg_catalog.pg_stat_activity
    WHERE wait_event = 'SyncRep'
)
SELECT v.property, v.value
FROM policy p CROSS JOIN senders s CROSS JOIN slots r CROSS JOIN waits w
CROSS JOIN LATERAL (VALUES
    (1, 'Collected at', clock_timestamp()::text),
    (2, 'Server role', CASE WHEN p.in_recovery AND s.total > 0 THEN 'cascading standby'
                           WHEN p.in_recovery THEN 'standby' ELSE 'primary' END),
    (3, 'Collector database', current_database()::text),
    (4, 'Collector role', current_user::text),
    (5, 'Statistics visibility', CASE WHEN p.full_stats THEN 'full'
         ELSE 'restricted: sender details and SyncRep wait counts may be hidden' END),
    (6, 'Synchronous standby policy', CASE
         WHEN p.sync_names = '' THEN 'disabled'
         WHEN p.sync_names ~* '^ANY\s+[0-9]+\s*\(' THEN 'quorum (ANY)'
         ELSE 'priority (FIRST)' END),
    (7, 'synchronous_standby_names', p.sync_names),
    (8, 'Required synchronous standbys', CASE WHEN p.sync_names = '' THEN '0'
         ELSE coalesce(substring(p.sync_names FROM
              '(?i)^(?:(?:FIRST|ANY)\s+)?([0-9]+)\s*\('), '1') END),
    (9, 'synchronous_commit (collector session)', p.sync_commit),
    (10, 'Remote commit acknowledgement (collector session)', CASE
         WHEN p.in_recovery THEN 'not applicable on a standby'
         WHEN p.sync_names = '' THEN 'no synchronous standby wait configured'
         WHEN p.sync_commit IN ('off', 'local') THEN 'no remote commit wait'
         WHEN p.sync_commit = 'remote_write' THEN 'remote WAL write'
         WHEN p.sync_commit = 'remote_apply' THEN 'remote WAL replay'
         ELSE 'remote WAL flush' END),
    (11, 'Connected WAL senders', s.total::text),
    (12, 'Streaming WAL senders', CASE WHEN p.full_stats THEN s.streaming::text END),
    (13, 'Current synchronous senders', CASE WHEN p.full_stats THEN s.sync::text END),
    (14, 'Current quorum senders', CASE WHEN p.full_stats THEN s.quorum::text END),
    (15, 'Physical slots', r.physical::text),
    (16, 'Logical slots', r.logical::text),
    (17, 'Inactive slots', r.inactive::text),
    (18, 'Sessions waiting in SyncRep', CASE WHEN p.full_stats THEN w.sync_rep_waiters::text END)
) AS v(position, property, value)
ORDER BY v.position;
