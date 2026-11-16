-- Query A: authorized single-case retrieval (the main statement of get_case()).
--
-- Statement text: src/authorized_case_operations.py, AuthorizedCaseOperations.get_case(),
-- second conn.execute(). psycopg named parameters are replaced by positional
-- ones in order of first appearance: %(actor_id)s -> $1, %(roles)s -> $2,
-- %(case_id)s -> $3. check_sql_matches_source.py verifies the body below
-- against the source, ignoring whitespace only.
--
-- The application sends this statement once on a new autocommit connection,
-- with bound parameters, so PostgreSQL plans it with the actual values (a
-- custom plan). The first EXECUTE of a prepared statement is also a custom
-- plan, so this reproduces that planning.
--
-- Synthetic parameters (fixture.sql):
--   $1 actor   md5('eaiso-day14-actor-1')     active provider service_engineer,
--                                             active membership in all three orgs
--   $2 roles   policy.roles_permitting('case:read'), sorted
--   $3 case    md5('eaiso-day14-case-25000')  owned by baseline-large

PREPARE get_case_authorized(uuid, text[], uuid) AS
SELECT c.id, c.organization_id, c.title, c.description,
       c.status, c.version, c.created_at, c.updated_at
  FROM case_record AS c
  JOIN organization_membership AS m
    ON m.organization_id = c.organization_id
   AND m.actor_id = $1
   AND m.is_active
   AND m.role = ANY($2)
  JOIN actor AS a
    ON a.id = m.actor_id
   AND a.is_active
 WHERE c.id = $3;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT TEXT)
EXECUTE get_case_authorized(
    '1a1e8387-9f2a-572f-3ade-0f8211c630f8',
    '{service_engineer,service_manager}',
    '7dd97bf9-c05f-8ef8-ab05-3809f2576dc7');

DEALLOCATE get_case_authorized;
