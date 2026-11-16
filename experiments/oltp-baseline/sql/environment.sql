-- Day 14 OLTP baseline: environment, schema, statistics and dataset evidence.
-- Read-only. Run after fixture.sql and VACUUM (ANALYZE), with psql -X.

\pset pager off
\pset footer on

\echo '## Server and session'
SELECT now() AS captured_at,
       version(),
       current_setting('server_version_num') AS server_version_num,
       current_database() AS database,
       current_schema() AS schema,
       current_user AS role,
       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS role_is_superuser,
       pg_postmaster_start_time() AS postmaster_started;

\echo '## Installed extensions'
SELECT extname, extversion FROM pg_extension ORDER BY extname;

\echo '## Planner, memory and maintenance settings (selected)'
SELECT name, setting, unit, source
  FROM pg_settings
 WHERE name IN ('shared_buffers', 'work_mem', 'effective_cache_size', 'maintenance_work_mem',
                'random_page_cost', 'seq_page_cost', 'cpu_tuple_cost', 'cpu_index_tuple_cost',
                'cpu_operator_cost', 'effective_io_concurrency', 'default_statistics_target',
                'jit', 'jit_above_cost', 'max_parallel_workers_per_gather', 'max_parallel_workers',
                'max_worker_processes', 'min_parallel_table_scan_size',
                'min_parallel_index_scan_size', 'parallel_setup_cost', 'parallel_tuple_cost',
                'plan_cache_mode', 'from_collapse_limit', 'join_collapse_limit',
                'autovacuum', 'track_io_timing', 'max_connections', 'server_encoding',
                'lc_collate', 'TimeZone', 'default_transaction_isolation',
                'huge_pages', 'wal_level', 'fsync', 'synchronous_commit')
    OR name LIKE 'enable\_%'
 ORDER BY name;

\echo '## All settings not at their built-in default'
SELECT name, setting, unit, source
  FROM pg_settings
 WHERE source NOT IN ('default', 'override')
 ORDER BY name;

\echo '## Effective indexes in schema public (including constraint-backed indexes)'
SELECT t.relname AS table_name,
       i.relname AS index_name,
       am.amname AS method,
       x.indisprimary AS is_primary,
       x.indisunique AS is_unique,
       con.conname AS backing_constraint,
       pg_get_indexdef(x.indexrelid) AS definition,
       pg_relation_size(x.indexrelid) AS size_bytes
  FROM pg_index AS x
  JOIN pg_class AS i ON i.oid = x.indexrelid
  JOIN pg_class AS t ON t.oid = x.indrelid
  JOIN pg_namespace AS n ON n.oid = t.relnamespace
  JOIN pg_am AS am ON am.oid = i.relam
  -- Only the constraint the index implements (primary key / unique /
  -- exclusion), not foreign keys that merely reference it.
  LEFT JOIN pg_constraint AS con
         ON con.conindid = x.indexrelid
        AND con.conrelid = x.indrelid
        AND con.contype IN ('p', 'u', 'x')
 WHERE n.nspname = 'public'
 ORDER BY t.relname, i.relname;

\echo '## Exact row counts (actual, not intended)'
SELECT 'organization' AS table_name, count(*) AS row_count FROM organization
UNION ALL SELECT 'actor', count(*) FROM actor
UNION ALL SELECT 'organization_membership', count(*) FROM organization_membership
UNION ALL SELECT 'case_record', count(*) FROM case_record
UNION ALL SELECT 'case_status_event', count(*) FROM case_status_event
UNION ALL SELECT 'ai_suggestion', count(*) FROM ai_suggestion
UNION ALL SELECT 'case_creation_idempotency', count(*) FROM case_creation_idempotency
ORDER BY 1;

\echo '## Cases per organization'
SELECT o.slug, count(c.id) AS cases, min(c.created_at) AS first_created, max(c.created_at) AS last_created
  FROM organization AS o
  LEFT JOIN case_record AS c ON c.organization_id = o.id
 GROUP BY o.slug
 ORDER BY cases DESC;

\echo '## Actors and memberships'
SELECT affiliation_type, is_active, count(*) AS actors
  FROM actor GROUP BY 1, 2 ORDER BY 1, 2;
SELECT o.slug, m.role, m.is_active, count(*) AS memberships
  FROM organization_membership AS m
  JOIN organization AS o ON o.id = m.organization_id
 GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

\echo '## Membership eligibility violations (expected 0 rows)'
SELECT count(*) AS violations FROM organization_membership_eligibility_violation;

\echo '## Physical size and planner statistics'
SELECT c.relname AS table_name, c.relpages, c.reltuples::bigint AS reltuples,
       pg_relation_size(c.oid) AS heap_bytes,
       pg_total_relation_size(c.oid) AS total_bytes
  FROM pg_class AS c
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r'
 ORDER BY c.relname;

SELECT relname, n_live_tup, n_dead_tup, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
  FROM pg_stat_user_tables ORDER BY relname;

SELECT tablename, attname, null_frac, n_distinct, correlation
  FROM pg_stats
 WHERE schemaname = 'public'
   AND tablename IN ('case_record', 'organization_membership')
   AND attname IN ('id', 'organization_id', 'created_at', 'actor_id', 'role', 'is_active')
 ORDER BY tablename, attname;

\echo '## Measured-query parameter precheck'
SELECT 'actor 1 active provider engineer' AS check_name,
       count(*) = 1 AS ok
  FROM actor
 WHERE id = '1a1e8387-9f2a-572f-3ade-0f8211c630f8' AND is_active AND affiliation_type = 'provider'
UNION ALL
SELECT 'actor 1 active service_engineer membership in baseline-large',
       count(*) = 1
  FROM organization_membership
 WHERE actor_id = '1a1e8387-9f2a-572f-3ade-0f8211c630f8'
   AND organization_id = '8af08632-4147-4fce-49b8-0f34277f37f1'
   AND is_active AND role = 'service_engineer'
UNION ALL
SELECT 'actor 1 active service_engineer membership in baseline-small',
       count(*) = 1
  FROM organization_membership
 WHERE actor_id = '1a1e8387-9f2a-572f-3ade-0f8211c630f8'
   AND organization_id = 'ba163064-53d2-177c-e4e6-611ea72ea20e'
   AND is_active AND role = 'service_engineer'
UNION ALL
SELECT 'case 25000 exists in baseline-large',
       count(*) = 1
  FROM case_record
 WHERE id = '7dd97bf9-c05f-8ef8-ab05-3809f2576dc7'
   AND organization_id = '8af08632-4147-4fce-49b8-0f34277f37f1';
