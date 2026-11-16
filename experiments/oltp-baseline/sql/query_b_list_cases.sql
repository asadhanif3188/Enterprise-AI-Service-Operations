-- Query B: organization-scoped case listing (the single statement of list_cases()).
--
-- Statement text: src/authorized_case_operations.py, AuthorizedCaseOperations.list_cases().
-- psycopg named parameters become positional in order of first appearance:
-- %(actor_id)s -> $1, %(org_id)s -> $2, %(roles)s -> $3, %(fetch)s -> $4.
-- check_sql_matches_source.py verifies the body below against the source,
-- ignoring whitespace only.
--
-- Effective requested limit 20 (DEFAULT_LIST_LIMIT): the application fetches
-- page_size + 1 = 21 rows to compute has_more. psycopg 3.3.9 binds the Python
-- int 21 as int2, declared as smallint here.
--
-- Synthetic parameters (fixture.sql):
--   $1 actor  md5('eaiso-day14-actor-1')  active provider service_engineer
--   $2 org    psql variable org_id; default md5('eaiso-day14-org-1') =
--             baseline-large (40,000 cases). run_baseline.sh also runs a
--             supplementary pass with baseline-small (1,000 cases).
--   $3 roles  policy.roles_permitting('case:list'), sorted
--   $4 fetch  21

\if :{?org_id}
\else
\set org_id 8af08632-4147-4fce-49b8-0f34277f37f1
\endif

PREPARE list_cases_authorized(uuid, uuid, text[], smallint) AS
SELECT s.actor_active, g.authorized,
       c.id, c.organization_id, c.title, c.status, c.version, c.created_at
  FROM (SELECT (SELECT is_active FROM actor WHERE id = $1)
               AS actor_active) AS s
 CROSS JOIN LATERAL (
       SELECT EXISTS (
           SELECT 1
             FROM actor AS a
             JOIN organization_membership AS m ON m.actor_id = a.id
            WHERE a.id = $1
              AND a.is_active
              AND m.organization_id = $2
              AND m.is_active
              AND m.role = ANY($3)
       ) AS authorized) AS g
  LEFT JOIN LATERAL (
       SELECT c.id, c.organization_id, c.title, c.status,
              c.version, c.created_at
         FROM case_record AS c
        WHERE g.authorized
          AND c.organization_id = $2
        ORDER BY c.created_at, c.id
        LIMIT $4) AS c ON TRUE
 ORDER BY c.created_at, c.id;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT TEXT)
EXECUTE list_cases_authorized(
    '1a1e8387-9f2a-572f-3ade-0f8211c630f8',
    :'org_id',
    '{service_engineer,service_manager}',
    21);

DEALLOCATE list_cases_authorized;
