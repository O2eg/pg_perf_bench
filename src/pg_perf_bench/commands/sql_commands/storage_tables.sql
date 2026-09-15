-- Measure every stored user table before limiting: relpages can be stale after the workload.
WITH sizes AS (
    SELECT c.oid::bigint AS table_oid,
           n.nspname::text AS schema_name,
           c.relname::text AS table_name,
           c.relkind::text AS relation_kind,
           c.relispartition AS is_partition,
           pg_catalog.pg_table_size(c.oid) AS table_size_bytes,
           pg_catalog.pg_indexes_size(c.oid) AS indexes_size_bytes,
           CASE WHEN c.reltoastrelid <> 0
                THEN pg_catalog.pg_total_relation_size(c.reltoastrelid)
                ELSE 0::bigint END AS toast_size_bytes,
           pg_catalog.pg_total_relation_size(c.oid) AS total_size_bytes
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_(toast|temp)_'
      AND n.nspname <> 'pg_toast'
)
SELECT *, pg_catalog.pg_size_pretty(total_size_bytes) AS total_size
FROM sizes
ORDER BY total_size_bytes DESC NULLS LAST, schema_name, table_name, table_oid
LIMIT 100;
