-- Actor identity, provider/customer affiliation, and customer access grants.
-- Apply once, after db/001_init.sql and db/002_case_status_events.sql.
-- Do not alter historical migration files.
--
-- Implements the schema and database guards accepted in
-- docs/adr/0006-actor-affiliation-and-role-grant-eligibility.md, on top of the
-- identity model accepted in docs/adr/0005-server-verified-identity-and-tenant-authorization.md.
--
-- Scope and limits:
--   * This migration adds no authentication, credential storage, request-time
--     authorization, row-level security, or database-role separation.
--   * Nothing in src/case_service.py reads these tables; case operations are
--     not protected by this migration.
--   * The guards below do not stop a superuser, a table owner who disables
--     triggers, or a session with session_replication_role = replica.
--   * The trigger functions are SECURITY INVOKER: a future least-privilege
--     writer role needs privileges sufficient for their SELECT ... FOR SHARE
--     on actor and SELECT on organization_membership.
--   * The fictional provider (ABC) is implicit and is NOT a row in organization.
BEGIN;

-- ---------------------------------------------------------------------------
-- Principals. One row per human actor of the synthetic demonstration.
-- ---------------------------------------------------------------------------
CREATE TABLE actor (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Synthetic label for display only. Not unique, not login proof, and never
    -- an authorization input.
    display_name TEXT NOT NULL,

    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    -- Business affiliation, set only by trusted provisioning. Not, by itself,
    -- an access grant.
    affiliation_type TEXT NOT NULL,

    -- The customer a customer-affiliated actor belongs to; NULL for provider
    -- staff because the provider has no organization row.
    home_organization_id UUID NULL
        REFERENCES organization(id) ON DELETE RESTRICT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT actor_display_name_nonblank
        CHECK (BTRIM(display_name) <> ''),

    CONSTRAINT actor_affiliation_type_valid
        CHECK (affiliation_type IN ('provider', 'customer')),

    -- Same-row shape check only. Cross-table eligibility is enforced by the
    -- triggers below, because a CHECK constraint must not query other tables.
    CONSTRAINT actor_affiliation_consistent
        CHECK (
            (affiliation_type = 'provider' AND home_organization_id IS NULL)
         OR (affiliation_type = 'customer' AND home_organization_id IS NOT NULL)
        )
);

-- ---------------------------------------------------------------------------
-- Customer access grants. A row grants one role in one customer organization;
-- it is not employment by that customer. One role per actor per organization.
-- ---------------------------------------------------------------------------
CREATE TABLE organization_membership (
    actor_id UUID NOT NULL
        REFERENCES actor(id) ON DELETE RESTRICT,

    organization_id UUID NOT NULL
        REFERENCES organization(id) ON DELETE RESTRICT,

    role TEXT NOT NULL,

    -- An inactive grant is a non-authoritative historical record.
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT organization_membership_pkey
        PRIMARY KEY (actor_id, organization_id),

    -- platform_administrator is deliberately not a membership role.
    CONSTRAINT organization_membership_role_valid
        CHECK (role IN ('service_engineer', 'service_manager', 'requester'))
);

-- No secondary indexes yet (identity model, section 4): the primary key serves
-- actor-first lookups. Without an index on organization_id (and on
-- actor.home_organization_id), the ON DELETE RESTRICT check when deleting an
-- organization scans these tables; acceptable at synthetic scale. Add indexes
-- from measured plans.

-- ---------------------------------------------------------------------------
-- ADR-0006 role-grant eligibility matrix, as one pure function shared by both
-- guards and by the integrity view. Every combination not listed is denied:
--   provider -> service_engineer | service_manager, in any explicitly granted
--               customer organization
--   customer -> requester, only in home_organization_id
-- ---------------------------------------------------------------------------
CREATE FUNCTION role_grant_is_eligible(
    p_affiliation_type TEXT,
    p_home_organization_id UUID,
    p_organization_id UUID,
    p_role TEXT
)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT COALESCE(
        CASE p_affiliation_type
            WHEN 'provider' THEN p_role IN ('service_engineer', 'service_manager')
            WHEN 'customer' THEN p_role = 'requester'
                             AND p_organization_id = p_home_organization_id
        END,
        FALSE
    )
$$;

-- ---------------------------------------------------------------------------
-- Membership-side guard.
--
-- Validates every INSERT (active or inactive), every UPDATE that changes the
-- actor, organization, or role, and every UPDATE that leaves the grant active
-- (which includes reactivation). A pure deactivation is never blocked, so
-- incompatible grants can always be revoked.
--
-- The actor row is locked FOR SHARE before its affiliation is read. FOR SHARE
-- conflicts with the FOR NO KEY UPDATE lock taken by an UPDATE of actor, so a
-- concurrent affiliation change either commits first (and this read, under
-- READ COMMITTED, then sees the new affiliation) or waits for this
-- transaction. The FOR KEY SHARE lock taken by the actor_id foreign key does
-- not conflict with non-key updates and would not be sufficient.
--
-- Isolation: the guard requires READ COMMITTED (ADR-0005 write transactions).
-- Under REPEATABLE READ or SERIALIZABLE the actor-side query would use the
-- transaction snapshot and could miss a grant committed after it, so writes
-- that need validation are rejected at higher isolation levels.
--
-- Trigger ordering: BEFORE ROW triggers fire in name order. Any trigger added
-- later to these tables must not change role, organization_id, actor_id, or
-- is_active after this guard has run.
--
-- Lock order: for an UPDATE, PostgreSQL locks the membership row before this
-- BEFORE trigger runs, so the trigger's actor lock is taken second. Trusted
-- provisioning therefore locks the actor first, explicitly. Writers that
-- bypass that protocol can deadlock; PostgreSQL then aborts one transaction,
-- which must be treated as a failed write.
-- ---------------------------------------------------------------------------
CREATE FUNCTION organization_membership_enforce_eligibility()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_affiliation_type TEXT;
    v_home_organization_id UUID;
BEGIN
    IF TG_OP = 'UPDATE'
       AND NOT NEW.is_active
       AND NEW.actor_id = OLD.actor_id
       AND NEW.organization_id = OLD.organization_id
       AND NEW.role = OLD.role THEN
        -- Deactivation, or a no-op on an inactive historical grant.
        RETURN NEW;
    END IF;

    IF NEW.actor_id IS NULL OR NEW.organization_id IS NULL OR NEW.role IS NULL
       OR NEW.role NOT IN ('service_engineer', 'service_manager', 'requester') THEN
        -- Let the NOT NULL constraints or organization_membership_role_valid
        -- report the problem. They are checked after BEFORE triggers and
        -- always reject these rows.
        RETURN NEW;
    END IF;

    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'organization_membership grant writes require READ COMMITTED isolation (current: %)',
                current_setting('transaction_isolation')
            USING ERRCODE = 'object_not_in_prerequisite_state',
                  HINT = 'Run identity provisioning in a READ COMMITTED transaction.';
    END IF;

    SELECT a.affiliation_type, a.home_organization_id
      INTO v_affiliation_type, v_home_organization_id
      FROM public.actor AS a
     WHERE a.id = NEW.actor_id
       FOR SHARE;

    IF NOT FOUND THEN
        -- Let the actor_id foreign key report the missing actor (SQLSTATE 23503).
        RETURN NEW;
    END IF;

    IF NOT public.role_grant_is_eligible(
        v_affiliation_type, v_home_organization_id, NEW.organization_id, NEW.role
    ) THEN
        RAISE EXCEPTION 'role-grant eligibility violation: % actor cannot hold a % grant in organization %',
                v_affiliation_type, NEW.role, NEW.organization_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'organization_membership_role_eligible',
                  SCHEMA = 'public',
                  TABLE = 'organization_membership',
                  DETAIL = format('actor_id=%s affiliation_type=%s home_organization_id=%s',
                                  NEW.actor_id, v_affiliation_type,
                                  COALESCE(v_home_organization_id::text, 'NULL')),
                  HINT = 'Provider actors may hold service_engineer or service_manager grants; customer actors may hold only a requester grant in their home organization.';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER organization_membership_role_eligible
    BEFORE INSERT OR UPDATE ON organization_membership
    FOR EACH ROW
    EXECUTE FUNCTION organization_membership_enforce_eligibility();

-- ---------------------------------------------------------------------------
-- Actor-side guard.
--
-- Rejects a change of affiliation_type or home_organization_id while the actor
-- holds any ACTIVE grant that would become ineligible. Grants are never
-- deleted or rewritten here; they must be deactivated first. Inactive
-- historical grants are ignored and are revalidated on reactivation.
--
-- Under READ COMMITTED this query sees committed grants only. An uncommitted
-- grant insert or reactivation holds FOR SHARE on this actor (membership-side
-- guard), and this UPDATE's row lock waits for it; the query below runs after
-- that wait with a fresh snapshot and so sees the committed grant.
-- ---------------------------------------------------------------------------
CREATE FUNCTION actor_enforce_grant_eligibility()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_organization_id UUID;
    v_role TEXT;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        -- A REPEATABLE READ or SERIALIZABLE snapshot could miss a grant that
        -- committed while this UPDATE waited for the actor row lock.
        RAISE EXCEPTION 'actor affiliation changes require READ COMMITTED isolation (current: %)',
                current_setting('transaction_isolation')
            USING ERRCODE = 'object_not_in_prerequisite_state',
                  HINT = 'Run identity provisioning in a READ COMMITTED transaction.';
    END IF;

    SELECT m.organization_id, m.role
      INTO v_organization_id, v_role
      FROM public.organization_membership AS m
     WHERE m.actor_id = NEW.id
       AND m.is_active
       AND NOT public.role_grant_is_eligible(
               NEW.affiliation_type, NEW.home_organization_id,
               m.organization_id, m.role)
     ORDER BY m.organization_id
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION 'affiliation change rejected: actor % would keep an ineligible active % grant in organization %',
                NEW.id, v_role, v_organization_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'actor_grants_remain_eligible',
                  SCHEMA = 'public',
                  TABLE = 'actor',
                  DETAIL = format('new affiliation_type=%s home_organization_id=%s',
                                  NEW.affiliation_type,
                                  COALESCE(NEW.home_organization_id::text, 'NULL')),
                  HINT = 'Deactivate or reconcile incompatible active grants before changing affiliation.';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER actor_grants_remain_eligible
    BEFORE UPDATE ON actor
    FOR EACH ROW
    WHEN (OLD.affiliation_type IS DISTINCT FROM NEW.affiliation_type
       OR OLD.home_organization_id IS DISTINCT FROM NEW.home_organization_id)
    EXECUTE FUNCTION actor_enforce_grant_eligibility();

-- ---------------------------------------------------------------------------
-- Integrity check for data that did not pass through the triggers, for
-- example rows restored by pg_dump (which creates triggers after loading
-- data) or written while triggers were disabled. Expected to be empty.
-- ---------------------------------------------------------------------------
CREATE VIEW organization_membership_eligibility_violation AS
SELECT m.actor_id,
       m.organization_id,
       m.role,
       a.affiliation_type,
       a.home_organization_id
  FROM organization_membership AS m
  JOIN actor AS a ON a.id = m.actor_id
 WHERE m.is_active
   AND NOT role_grant_is_eligible(
           a.affiliation_type, a.home_organization_id,
           m.organization_id, m.role);

COMMIT;
