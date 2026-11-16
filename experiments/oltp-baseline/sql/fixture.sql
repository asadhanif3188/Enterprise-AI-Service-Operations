-- Day 14 OLTP baseline: deterministic synthetic fixture.
--
-- Apply once to an EMPTY database that has db/001 to db/004 applied, with
-- psql -v ON_ERROR_STOP=1. Every value is fictional and derived from a fixed
-- formula: rerunning on a fresh database produces byte-identical identifiers,
-- text and timestamps. No randomness, no real customers, no credentials.
--
-- Identifiers: md5('eaiso-day14-<kind>-<n>')::uuid, so the measured query
-- parameters are known in advance (see the README).
--
-- Distribution (intended; actual counts are verified by sql/environment.sql):
--   organizations   3   baseline-large, baseline-medium, baseline-small
--   case_record 50,000  n = 1..50,000; org by n % 50: 0-39 large (40,000),
--                       40-48 medium (9,000), 49 small (1,000). Orgs are
--                       interleaved in creation order. created_at =
--                       2026-01-01 00:00 UTC + n * 10 minutes, so physical
--                       insertion order equals created_at order and created_at
--                       is unique. All status NEW, version 1 (no status events).
--   actor          37   7 provider actors (1-3 engineers, 4-6 managers, 7 an
--                       INACTIVE engineer) + 10 customer requesters per org.
--   organization_membership 49
--                       providers 1-6 in all 3 orgs (18; actor 6 in the small
--                       org is INACTIVE), actor 7 in the large org (1),
--                       each requester in its home org (30).
--   ai_suggestion, case_status_event, case_creation_idempotency: 0 rows.
--
-- Memberships are inserted through the db/003 eligibility trigger (READ
-- COMMITTED, the psql default), exactly as trusted provisioning would.

BEGIN;

DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM organization) OR EXISTS (SELECT 1 FROM actor)
       OR EXISTS (SELECT 1 FROM case_record) THEN
        RAISE EXCEPTION 'fixture.sql requires an empty database';
    END IF;
END
$guard$;

INSERT INTO organization (id, name, slug, created_at)
SELECT md5('eaiso-day14-org-' || v.k)::uuid, v.name, v.slug,
       TIMESTAMPTZ '2026-01-01 00:00:00+00'
  FROM (VALUES (1, 'Baseline Large Customer (synthetic)',  'baseline-large'),
               (2, 'Baseline Medium Customer (synthetic)', 'baseline-medium'),
               (3, 'Baseline Small Customer (synthetic)',  'baseline-small')) AS v(k, name, slug);

-- Provider actors: no home organization (the provider has no organization row).
INSERT INTO actor (id, display_name, is_active, affiliation_type, home_organization_id, created_at)
SELECT md5('eaiso-day14-actor-' || k)::uuid,
       'Synthetic provider ' || k,
       k <> 7,
       'provider', NULL,
       TIMESTAMPTZ '2026-01-01 00:00:00+00'
  FROM generate_series(1, 7) AS k;

-- Customer requesters: actor key o*100 + j, home organization o.
INSERT INTO actor (id, display_name, is_active, affiliation_type, home_organization_id, created_at)
SELECT md5('eaiso-day14-actor-' || (o * 100 + j))::uuid,
       'Synthetic requester ' || o || '-' || j,
       TRUE,
       'customer', md5('eaiso-day14-org-' || o)::uuid,
       TIMESTAMPTZ '2026-01-01 00:00:00+00'
  FROM generate_series(1, 3) AS o, generate_series(1, 10) AS j;

INSERT INTO organization_membership (actor_id, organization_id, role, is_active, created_at)
SELECT md5('eaiso-day14-actor-' || k)::uuid,
       md5('eaiso-day14-org-' || o)::uuid,
       CASE WHEN k <= 3 THEN 'service_engineer' ELSE 'service_manager' END,
       NOT (k = 6 AND o = 3),
       TIMESTAMPTZ '2026-01-01 00:00:00+00'
  FROM generate_series(1, 6) AS k, generate_series(1, 3) AS o;

INSERT INTO organization_membership (actor_id, organization_id, role, is_active, created_at)
VALUES (md5('eaiso-day14-actor-7')::uuid, md5('eaiso-day14-org-1')::uuid,
        'service_engineer', TRUE, TIMESTAMPTZ '2026-01-01 00:00:00+00');

INSERT INTO organization_membership (actor_id, organization_id, role, is_active, created_at)
SELECT md5('eaiso-day14-actor-' || (o * 100 + j))::uuid,
       md5('eaiso-day14-org-' || o)::uuid,
       'requester', TRUE,
       TIMESTAMPTZ '2026-01-01 00:00:00+00'
  FROM generate_series(1, 3) AS o, generate_series(1, 10) AS j;

INSERT INTO case_record (id, organization_id, title, description, status, version,
                         created_at, updated_at)
SELECT md5('eaiso-day14-case-' || n)::uuid,
       md5('eaiso-day14-org-' || CASE WHEN n % 50 < 40 THEN 1
                                      WHEN n % 50 < 49 THEN 2
                                      ELSE 3 END)::uuid,
       'Synthetic baseline case ' || n,
       'Synthetic description for baseline case ' || n || '.'
           || repeat(' Additional synthetic detail.', n % 20),
       'NEW',
       1,
       TIMESTAMPTZ '2026-01-01 00:00:00+00' + n * INTERVAL '10 minutes',
       TIMESTAMPTZ '2026-01-01 00:00:00+00' + n * INTERVAL '10 minutes'
  FROM generate_series(1, 50000) AS n;

COMMIT;
