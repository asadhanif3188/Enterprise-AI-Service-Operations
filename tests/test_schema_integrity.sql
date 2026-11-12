-- Relational integrity tests for the initial PostgreSQL schema.
-- Prerequisites: db/001_init.sql and db/seed_dev.sql have been executed.
-- Run with psql -v ON_ERROR_STOP=1. All probe writes are rolled back.

BEGIN;

DO $schema_integrity$
DECLARE
    probe_org_id UUID;
    probe_case_id UUID;
    seeded_count INTEGER;
    actual_constraint TEXT;
BEGIN
    -- 1. Confirm that development seed data is available.
    SELECT COUNT(DISTINCT o.slug) INTO seeded_count
    FROM organization AS o
    JOIN case_record AS c ON c.organization_id = o.id
    WHERE o.slug IN ('northwind', 'summit');

    IF seeded_count <> 2 THEN
        RAISE EXCEPTION 'FAIL seed data: expected cases for both northwind and summit, found % organization(s)', seeded_count;
    END IF;
    RAISE NOTICE 'PASS 01: synthetic seed organizations and cases exist';

    -- 2. Valid records should be accepted, with the expected defaults.
    INSERT INTO organization (name, slug)
    VALUES ('Schema Integrity Probe', '__schema_integrity_probe__')
    RETURNING id INTO probe_org_id;

    INSERT INTO case_record (organization_id, title, description)
    VALUES (probe_org_id, 'Integrity probe case', 'Synthetic data for schema integrity testing.')
    RETURNING id INTO probe_case_id;

    IF NOT EXISTS (
        SELECT 1 FROM case_record
        WHERE id = probe_case_id AND status = 'NEW' AND version = 1
    ) THEN
        RAISE EXCEPTION 'FAIL case defaults: expected NEW status and version 1';
    END IF;

    INSERT INTO ai_suggestion (
        case_id, case_version, proposed_category, proposed_urgency,
        reasoning, suggested_next_step, model_id, prompt_version
    )
    VALUES (
        probe_case_id, 1, 'network', 'low',
        'Synthetic test explanation.', 'Investigate network access.',
        'synthetic-test-model', 'test-v1'
    );

    IF NOT EXISTS (
        SELECT 1 FROM case_record WHERE id = probe_case_id AND status = 'NEW'
    ) THEN
        RAISE EXCEPTION 'FAIL AI boundary: inserting suggestion changed case status';
    END IF;
    RAISE NOTICE 'PASS 02: valid organization, case, suggestion, defaults, and unchanged case state';

    -- 3. Reject unknown case states.
    BEGIN
        INSERT INTO case_record (organization_id, title, description, status)
        VALUES (probe_org_id, 'Bad status', 'Synthetic test.', 'PENDING');
        RAISE EXCEPTION 'FAIL 03: invalid status was accepted';
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS actual_constraint = CONSTRAINT_NAME;
        IF actual_constraint <> 'case_valid_status' THEN
            RAISE EXCEPTION 'FAIL 03: wrong check constraint (%)', actual_constraint;
        END IF;
        RAISE NOTICE 'PASS 03: invalid case status rejected';
    END;

    -- 4. Reject cases without an owning organization.
    BEGIN
        INSERT INTO case_record (title, description)
        VALUES ('Missing organization', 'Synthetic test.');
        RAISE EXCEPTION 'FAIL 04: missing organization_id was accepted';
    EXCEPTION WHEN not_null_violation THEN
        RAISE NOTICE 'PASS 04: missing organization_id rejected';
    END;

    -- 5. Reject references to organizations that do not exist.
    BEGIN
        INSERT INTO case_record (organization_id, title, description)
        VALUES (gen_random_uuid(), 'Unknown organization', 'Synthetic test.');
        RAISE EXCEPTION 'FAIL 05: unknown organization_id was accepted';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'PASS 05: unknown organization reference rejected';
    END;

    -- 6. Reject a duplicate slug.
    BEGIN
        INSERT INTO organization (name, slug)
        VALUES ('Different Organization', '__schema_integrity_probe__');
        RAISE EXCEPTION 'FAIL 06: duplicate organization slug was accepted';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'PASS 06: duplicate organization slug rejected';
    END;

    -- 7. Reject blank case titles.
    BEGIN
        INSERT INTO case_record (organization_id, title, description)
        VALUES (probe_org_id, '   ', 'Synthetic test.');
        RAISE EXCEPTION 'FAIL 07: blank case title was accepted';
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS actual_constraint = CONSTRAINT_NAME;
        IF actual_constraint <> 'case_title_nonblank' THEN
            RAISE EXCEPTION 'FAIL 07: wrong check constraint (%)', actual_constraint;
        END IF;
        RAISE NOTICE 'PASS 07: blank case title rejected';
    END;

    -- 8. Reject a nonpositive case revision.
    BEGIN
        INSERT INTO case_record (organization_id, title, description, version)
        VALUES (probe_org_id, 'Bad revision', 'Synthetic test.', 0);
        RAISE EXCEPTION 'FAIL 08: version 0 was accepted';
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS actual_constraint = CONSTRAINT_NAME;
        IF actual_constraint <> 'case_positive_version' THEN
            RAISE EXCEPTION 'FAIL 08: wrong check constraint (%)', actual_constraint;
        END IF;
        RAISE NOTICE 'PASS 08: nonpositive case version rejected';
    END;

    -- 9. Reject suggestions that reference nonexistent cases.
    BEGIN
        INSERT INTO ai_suggestion (case_id, case_version, model_id, prompt_version)
        VALUES (gen_random_uuid(), 1, 'synthetic-test-model', 'test-v1');
        RAISE EXCEPTION 'FAIL 09: suggestion without a case was accepted';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'PASS 09: unknown suggestion case reference rejected';
    END;

    -- 10. Reject a nonpositive suggestion case revision.
    BEGIN
        INSERT INTO ai_suggestion (case_id, case_version, model_id, prompt_version)
        VALUES (probe_case_id, 0, 'synthetic-test-model', 'test-v1');
        RAISE EXCEPTION 'FAIL 10: suggestion case_version 0 was accepted';
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS actual_constraint = CONSTRAINT_NAME;
        IF actual_constraint <> 'suggestion_positive_case_version' THEN
            RAISE EXCEPTION 'FAIL 10: wrong check constraint (%)', actual_constraint;
        END IF;
        RAISE NOTICE 'PASS 10: nonpositive suggestion case_version rejected';
    END;

    -- 11. Ensure deleting a referenced organization is rejected.
    BEGIN
        DELETE FROM organization WHERE id = probe_org_id;
        RAISE EXCEPTION 'FAIL 11: referenced organization was deleted';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'PASS 11: referenced organization deletion rejected';
    END;

    RAISE NOTICE 'ALL 11 SCHEMA INTEGRITY CHECKS PASSED';
END;
$schema_integrity$;

-- No probe rows are retained, including the successful inserts.
ROLLBACK;
