-- Select only non-secret replication/WAL settings available on this PG version.
SELECT name, setting, unit, source, pending_restart
FROM pg_catalog.pg_settings
WHERE name IN (
    'wal_level', 'synchronous_standby_names', 'synchronous_commit',
    'max_wal_senders', 'max_replication_slots', 'wal_keep_segments', 'wal_keep_size',
    'max_slot_wal_keep_size', 'idle_replication_slot_timeout',
    'wal_sender_timeout', 'wal_receiver_timeout', 'wal_receiver_status_interval',
    'hot_standby', 'hot_standby_feedback', 'max_standby_streaming_delay',
    'max_standby_archive_delay', 'recovery_min_apply_delay', 'primary_slot_name',
    'max_logical_replication_workers', 'max_sync_workers_per_subscription',
    'max_parallel_apply_workers_per_subscription', 'max_worker_processes',
    'logical_decoding_work_mem', 'max_active_replication_origins',
    'sync_replication_slots', 'synchronized_standby_slots'
)
ORDER BY name;
