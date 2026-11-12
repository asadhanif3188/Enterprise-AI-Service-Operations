
BEGIN;

-- Customer organizations
CREATE TABLE organization (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT organization_name_nonblank
        CHECK (BTRIM(name) <> ''),

    CONSTRAINT organization_slug_nonblank
        CHECK (BTRIM(slug) <> '')
);

-- Authoritative service-case records
CREATE TABLE case_record (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    organization_id UUID NOT NULL
        REFERENCES organization(id) ON DELETE RESTRICT,

    title TEXT NOT NULL,
    description TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'NEW',
    version BIGINT NOT NULL DEFAULT 1,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT case_title_nonblank
        CHECK (BTRIM(title) <> ''),

    CONSTRAINT case_description_nonblank
        CHECK (BTRIM(description) <> ''),

    CONSTRAINT case_valid_status
        CHECK (
            status IN (
                'NEW',
                'IN_REVIEW',
                'RESOLVED',
                'CLOSED'
            )
        ),

    CONSTRAINT case_positive_version
        CHECK (version > 0)
);

-- AI-produced suggestions, not authoritative state
CREATE TABLE ai_suggestion (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    case_id UUID NOT NULL
        REFERENCES case_record(id) ON DELETE RESTRICT,

    case_version BIGINT NOT NULL,

    proposed_category TEXT,
    proposed_urgency TEXT,
    reasoning TEXT,
    suggested_next_step TEXT,

    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT suggestion_positive_case_version
        CHECK (case_version > 0),

    CONSTRAINT suggestion_model_nonblank
        CHECK (BTRIM(model_id) <> ''),

    CONSTRAINT suggestion_prompt_nonblank
        CHECK (BTRIM(prompt_version) <> '')
);

COMMIT;
