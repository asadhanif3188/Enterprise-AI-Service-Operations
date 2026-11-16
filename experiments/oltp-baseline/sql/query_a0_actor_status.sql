-- Context for Query A: the preceding actor-status statement of get_case().
--
-- get_case() first runs this on the same connection to distinguish an inactive
-- actor (403) before the authorized read. It is part of the real read path but
-- is NOT the authorized case read; Query A (query_a_get_case.sql) is.
-- Statement text: src/authorized_case_operations.py, get_case(), first
-- conn.execute(); %s -> $1.

PREPARE get_case_actor_status(uuid) AS
SELECT is_active FROM actor WHERE id = $1;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT TEXT)
EXECUTE get_case_actor_status('1a1e8387-9f2a-572f-3ade-0f8211c630f8');

DEALLOCATE get_case_actor_status;
