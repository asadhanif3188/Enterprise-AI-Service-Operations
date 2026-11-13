-- Initial case status transition history.
-- Apply once, after db/001_init.sql. Do not alter historical migration files.
BEGIN;

CREATE TABLE case_status_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES case_record(id) ON DELETE RESTRICT,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    from_version BIGINT NOT NULL,
    to_version BIGINT NOT NULL,
    actor_label TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT case_event_from_status_valid
        CHECK (from_status IN ('NEW', 'IN_REVIEW', 'RESOLVED', 'CLOSED')),
    CONSTRAINT case_event_to_status_valid
        CHECK (to_status IN ('NEW', 'IN_REVIEW', 'RESOLVED', 'CLOSED')),
    CONSTRAINT case_event_version_step
        CHECK (from_version > 0 AND to_version = from_version + 1),
    CONSTRAINT case_event_actor_nonblank
        CHECK (BTRIM(actor_label) <> ''),
    CONSTRAINT case_event_one_per_revision
        UNIQUE (case_id, to_version)
);

-- The UNIQUE(case_id, to_version) index also supports per-case history reads.

COMMIT;
