-- Include newly grown indexes even if their catalog statistics have not been refreshed.
WITH sizes AS (
    SELECT idx.oid::bigint AS index_oid,
           n.nspname::text AS schema_name,
           tbl.relname::text AS table_name,
           idx.relname::text AS index_name,
           am.amname::text AS access_method,
           i.indisprimary AS is_primary,
           i.indisvalid AS is_valid,
           pg_catalog.pg_table_size(idx.oid) AS index_size_bytes
    FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_catalog.pg_class tbl ON tbl.oid = i.indrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = idx.relnamespace
    JOIN pg_catalog.pg_am am ON am.oid = idx.relam
    WHERE idx.relkind = 'i' AND tbl.relkind IN ('r', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_(toast|temp)_'
      AND n.nspname <> 'pg_toast'
)
SELECT *, pg_catalog.pg_size_pretty(index_size_bytes) AS index_size
FROM sizes
ORDER BY index_size_bytes DESC NULLS LAST, schema_name, table_name, index_name, index_oid
LIMIT 100;
