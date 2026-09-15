-- Based on pg_diag WAL receiver / standby recovery items. Never select conninfo.
SELECT
    r.pid, r.status, r.slot_name,
    to_jsonb(r)->>'sender_host' AS sender_host,
    (to_jsonb(r)->>'sender_port')::integer AS sender_port,
    r.receive_start_lsn::text AS receive_start_lsn,
    coalesce(to_jsonb(r)->>'flushed_lsn', to_jsonb(r)->>'received_lsn') AS flushed_lsn,
    r.latest_end_lsn::text AS latest_end_lsn,
    r.last_msg_send_time::text AS last_msg_send_time,
    r.last_msg_receipt_time::text AS last_msg_receipt_time,
    pg_catalog.pg_last_wal_receive_lsn()::text AS last_receive_lsn,
    pg_catalog.pg_last_wal_replay_lsn()::text AS last_replay_lsn,
    pg_catalog.pg_wal_lsn_diff(
        pg_catalog.pg_last_wal_receive_lsn(), pg_catalog.pg_last_wal_replay_lsn()
    )::bigint AS receive_to_replay_bytes
FROM pg_catalog.pg_stat_wal_receiver r
ORDER BY r.pid;
