-- Keyed idempotent case creation (ADR-0004).
-- Apply once, after db/001 to db/003. Do not alter historical migration files.
--
-- Implements the record accepted in docs/adr/0004-keyed-idempotent-case-creation.md:
-- one row per keyed create, scoped to the AUTHORIZED organization and the
-- VERIFIED actor, holding only the request fingerprint (digest and format
-- version, never the payload) and the resulting case.
--
-- Scope and limits:
--   * Rows never expire (local demonstration only; ADR-0004 rule 10). Production
--     use needs a finite retention policy, cleanup, and post-expiry semantics.
--   * The authorized-case-operations layer writes these rows inside the same
--     READ COMMITTED transaction as the case, after the ADR-0005 actor and
--     membership locks. The primary key arbitrates concurrent requests.
--   * Like the other guards, these constraints do not stop a superuser or a
--     table owner who drops them. No row-level security is added.
--   * No historical rows are created.
BEGIN;

-- Lets the idempotency record reference (organization_id, id), so a record can
-- only point to a case owned by the record's own organization. id is already
-- the primary key; this adds a second index whose only purpose is that
-- composite foreign key.
ALTER TABLE case_record
    ADD CONSTRAINT case_record_organization_case_key UNIQUE (organization_id, id);

CREATE TABLE case_creation_idempotency (
    organization_id UUID NOT NULL
        REFERENCES organization(id) ON DELETE RESTRICT,

    actor_id UUID NOT NULL
        REFERENCES actor(id) ON DELETE RESTRICT,

    operation TEXT NOT NULL,

    -- The decoded RFC 9651 String value, exactly as sent (case-sensitive).
    idempotency_key TEXT NOT NULL,

    fingerprint_version SMALLINT NOT NULL,

    -- Lowercase hexadecimal SHA-256 of the RFC 8785 canonical request.
    request_fingerprint TEXT NOT NULL,

    -- Every committed record names exactly one case (no pending state).
    case_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The ADR-0004 scope. The primary-key index arbitrates concurrent
    -- requests with the same scope.
    CONSTRAINT case_creation_idempotency_pkey
        PRIMARY KEY (organization_id, actor_id, operation, idempotency_key),

    CONSTRAINT case_creation_idempotency_operation_valid
        CHECK (operation IN ('case:create')),

    CONSTRAINT case_creation_idempotency_key_shape
        CHECK (idempotency_key ~ '^[A-Za-z0-9._~-]{16,128}$'),

    CONSTRAINT case_creation_idempotency_fingerprint_version_supported
        CHECK (fingerprint_version = 1),

    CONSTRAINT case_creation_idempotency_fingerprint_sha256_hex
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),

    -- One record per case.
    CONSTRAINT case_creation_idempotency_case_unique
        UNIQUE (case_id),

    -- The case must exist and belong to the record's organization. Deferred to
    -- commit so the layer can reserve the key (and wait on a competing
    -- reservation) before inserting the case in the same transaction. A case
    -- with a record cannot be deleted.
    CONSTRAINT case_creation_idempotency_case_owned_by_organization
        FOREIGN KEY (organization_id, case_id)
        REFERENCES case_record (organization_id, id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

COMMIT;
