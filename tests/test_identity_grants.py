"""Actor identity schema, ADR-0006 role-grant eligibility, provisioning, concurrency.

Offline tests need no database. Tests marked ``db`` require TEST_DATABASE_URL
pointing to the dedicated ``eaiso_test`` database with db/001, db/002, and
db/003 applied. Every database test creates its own uniquely named synthetic
organizations and actors and deletes them afterwards.

Concurrency tests coordinate real PostgreSQL sessions. A session is considered
blocked only when ``pg_blocking_pids`` names the expected holder. Every wait
is bounded (lock_timeout, statement_timeout, and future timeouts), so a
regression fails instead of hanging.
"""

from __future__ import annotations

import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from src import identity_provisioning as prov
from src.identity_provisioning import (
    CUSTOMER, PROVIDER, REQUESTER, SERVICE_ENGINEER, SERVICE_MANAGER,
)

WAIT_SECONDS = 10
SESSION_OPTIONS = "-c lock_timeout=10s -c statement_timeout=20s"


# =============================================================================
# Offline policy tests (no database)
# =============================================================================

A = uuid4()
B = uuid4()


@pytest.mark.parametrize("affiliation,home,org,role,allowed", [
    (PROVIDER, None, A, SERVICE_ENGINEER, True),
    (PROVIDER, None, B, SERVICE_MANAGER, True),
    (PROVIDER, None, A, REQUESTER, False),
    (CUSTOMER, A, A, REQUESTER, True),
    (CUSTOMER, A, B, REQUESTER, False),
    (CUSTOMER, A, A, SERVICE_ENGINEER, False),
    (CUSTOMER, A, A, SERVICE_MANAGER, False),
    (CUSTOMER, A, B, SERVICE_ENGINEER, False),
    (PROVIDER, None, A, "platform_administrator", False),
    ("contractor", None, A, SERVICE_ENGINEER, False),
    (CUSTOMER, None, A, REQUESTER, False),
])
def test_eligibility_matrix_offline(affiliation, home, org, role, allowed):
    assert prov.is_eligible(affiliation, home, org, role) is allowed
    if allowed:
        prov.check_eligibility(affiliation, home, org, role)
    else:
        with pytest.raises(prov.EligibilityError):
            prov.check_eligibility(affiliation, home, org, role)


@pytest.mark.parametrize("affiliation,home", [
    ("contractor", None), (PROVIDER, A), (CUSTOMER, None),
])
def test_affiliation_shape_rejected_offline(affiliation, home):
    with pytest.raises(ValueError):
        prov.check_affiliation_shape(affiliation, home)


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@127.0.0.1:5432/eaiso_prod",
    "postgresql://u:p@db.example.invalid:5432/eaiso_dev",
    "postgresql://u:p@127.0.0.1,db.example.invalid:5432/eaiso_test",
    "dbname=eaiso_test",
    "host=127.0.0.1 hostaddr=192.0.2.10 dbname=eaiso_test",
    "service=eaiso dbname=eaiso_test host=localhost",
])
def test_cli_refuses_non_local_or_unknown_database(dsn):
    from scripts.provision_dev_identities import check_local_target

    with pytest.raises(SystemExit):
        check_local_target(dsn)


def test_cli_accepts_local_test_database():
    from scripts.provision_dev_identities import check_local_target

    assert check_local_target("postgresql://u:p@127.0.0.1:5432/eaiso_test") == "eaiso_test"


@pytest.mark.parametrize("error_name,message", [
    ("DeadlockDetected", "Deadlock detected"),
    ("LockNotAvailable", "Lock timeout"),
    ("QueryCanceled", "Statement timeout"),
    ("SerializationFailure", "Serialization failure"),
    ("InternalError", "Database error"),
])
def test_database_failures_translate_to_provisioning_error_offline(error_name, message):
    from psycopg import errors

    translated = prov._translate(getattr(errors, error_name)("synthetic"))
    assert type(translated) is prov.ProvisioningError
    assert str(translated).startswith(message)
    assert "not applied" in str(translated)


# =============================================================================
# Database fixtures and helpers
# =============================================================================

@pytest.fixture
def identity_dsn():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for eaiso_test; database cases not executed")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    if conninfo_to_dict(dsn).get("dbname") != "eaiso_test":
        pytest.fail("Safety guard: TEST_DATABASE_URL must refer to eaiso_test")
    with psycopg.connect(dsn) as conn:
        name, grants = conn.execute(
            "SELECT current_database(), to_regclass('public.organization_membership')"
        ).fetchone()
    if name != "eaiso_test":
        pytest.fail("Safety guard: actual connected database is not eaiso_test")
    if grants is None:
        pytest.fail("Apply db/003_actor_identity_and_grants.sql to eaiso_test first")
    return dsn


class Workspace:
    """Isolated synthetic organizations, actors, and sessions for one test."""

    def __init__(self, dsn):
        import psycopg

        self.dsn = dsn
        self._psycopg = psycopg
        self.conns = []
        self.org_ids = []
        self.actor_ids = []
        self.admin = self.conn()
        self.org_a = self.new_org("a")
        self.org_b = self.new_org("b")

    def conn(self):
        c = self._psycopg.connect(self.dsn, autocommit=True, options=SESSION_OPTIONS)
        self.conns.append(c)
        return c

    def new_org(self, tag):
        org_id = self.admin.execute(
            "INSERT INTO organization (name, slug) VALUES (%s, %s) RETURNING id",
            (f"Fictional identity-test organization {tag}",
             f"identity-test-{tag}-{uuid4().hex}"),
        ).fetchone()[0]
        self.org_ids.append(org_id)
        return org_id

    def slug(self, org_id):
        return self.admin.execute(
            "SELECT slug FROM organization WHERE id = %s", (org_id,)).fetchone()[0]

    def provider(self, label="synthetic provider staff"):
        actor_id = prov.create_actor(self.admin, label, PROVIDER)
        self.actor_ids.append(actor_id)
        return actor_id

    def customer(self, home, label="synthetic customer requester"):
        actor_id = prov.create_actor(self.admin, label, CUSTOMER, home)
        self.actor_ids.append(actor_id)
        return actor_id

    def actor(self, actor_id):
        return self.admin.execute(
            "SELECT affiliation_type, home_organization_id, is_active FROM actor WHERE id = %s",
            (actor_id,),
        ).fetchone()

    def grants(self, actor_id):
        return [(g.organization_id, g.role, g.is_active)
                for g in prov.list_grants(self.admin, actor_id)]

    def violations(self):
        ids = list(self.actor_ids)
        return [v for v in prov.eligibility_violations(self.admin) if v[0] in ids]

    def close(self):
        for c in self.conns:
            try:
                c.close()
            except Exception:
                pass
        with self._psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(
                "DELETE FROM organization_membership WHERE actor_id = ANY(%s) "
                "OR organization_id = ANY(%s)", (self.actor_ids, self.org_ids))
            c.execute("DELETE FROM actor WHERE id = ANY(%s) OR home_organization_id = ANY(%s)",
                      (self.actor_ids, self.org_ids))
            c.execute("DELETE FROM organization WHERE id = ANY(%s)", (self.org_ids,))


@pytest.fixture
def ws(identity_dsn):
    workspace = Workspace(identity_dsn)
    try:
        yield workspace
    finally:
        workspace.close()


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(max_workers=4)
    try:
        yield executor
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def insert_grant(conn, actor_id, org_id, role, active=True):
    conn.execute(
        "INSERT INTO organization_membership (actor_id, organization_id, role, is_active) "
        "VALUES (%s, %s, %s, %s)", (actor_id, org_id, role, active))


def constraint_of(excinfo):
    return excinfo.value.diag.constraint_name


def pid(conn):
    return conn.info.backend_pid


def wait_until_blocked(monitor, waiter_pid, holder_pid, future=None):
    """Return once waiter_pid is blocked by holder_pid; fail after WAIT_SECONDS."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if future is not None and future.done():
            future.result()  # surface an unexpected early exception
            raise AssertionError("operation finished without waiting for the lock holder")
        blockers = monitor.execute("SELECT pg_blocking_pids(%s)", (waiter_pid,)).fetchone()[0]
        if holder_pid in blockers:
            return
        time.sleep(0.02)
    raise AssertionError(f"session {waiter_pid} was not blocked by {holder_pid}")


def outcome(future):
    """Classify a finished operation: 'ok', the exception class name, or constraint."""
    import psycopg

    try:
        future.result(timeout=WAIT_SECONDS + 10)
        return "ok"
    except psycopg.errors.CheckViolation as exc:
        return exc.diag.constraint_name
    except Exception as exc:  # noqa: BLE001 - classification only
        return type(exc).__name__


# =============================================================================
# Schema tests
# =============================================================================

@pytest.mark.db
def test_valid_provider_actor_has_generated_id_and_defaults(ws):
    actor_id = ws.provider()
    row = ws.admin.execute(
        "SELECT id, is_active, affiliation_type, home_organization_id, created_at "
        "FROM actor WHERE id = %s", (actor_id,)).fetchone()
    assert row[0] == actor_id and row[0].version == 4
    assert row[1:4] == (True, PROVIDER, None)
    assert row[4] is not None


@pytest.mark.db
def test_valid_customer_actor(ws):
    actor_id = ws.customer(ws.org_a)
    assert ws.actor(actor_id) == (CUSTOMER, ws.org_a, True)


@pytest.mark.db
@pytest.mark.parametrize("affiliation,home_key,expected", [
    # An unknown type violates both CHECKs; PostgreSQL reports one of them.
    ("contractor", None, {"actor_affiliation_type_valid", "actor_affiliation_consistent"}),
    (PROVIDER, "a", {"actor_affiliation_consistent"}),
    (CUSTOMER, None, {"actor_affiliation_consistent"}),
])
def test_invalid_actor_affiliation_rejected(ws, affiliation, home_key, expected):
    import psycopg

    home = ws.org_a if home_key == "a" else None
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        ws.admin.execute(
            "INSERT INTO actor (display_name, affiliation_type, home_organization_id) "
            "VALUES ('synthetic invalid actor', %s, %s)", (affiliation, home))
    assert constraint_of(excinfo) in expected
    assert ws.admin.execute(
        "SELECT count(*) FROM actor WHERE display_name = 'synthetic invalid actor'"
    ).fetchone()[0] == 0


@pytest.mark.db
def test_blank_display_name_and_missing_affiliation_rejected(ws):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        ws.admin.execute("INSERT INTO actor (display_name, affiliation_type) VALUES ('  ', 'provider')")
    assert constraint_of(excinfo) == "actor_display_name_nonblank"
    with pytest.raises(psycopg.errors.NotNullViolation):
        ws.admin.execute("INSERT INTO actor (display_name) VALUES ('synthetic no affiliation')")


@pytest.mark.db
def test_nonexistent_home_organization_rejected(ws):
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation) as excinfo:
        ws.admin.execute(
            "INSERT INTO actor (display_name, affiliation_type, home_organization_id) "
            "VALUES ('synthetic orphan', 'customer', %s)", (uuid4(),))
    assert constraint_of(excinfo) == "actor_home_organization_id_fkey"


@pytest.mark.db
def test_duplicate_membership_rejected(ws):
    import psycopg

    ali = ws.provider()
    insert_grant(ws.admin, ali, ws.org_a, SERVICE_ENGINEER)
    with pytest.raises(psycopg.errors.UniqueViolation) as excinfo:
        insert_grant(ws.admin, ali, ws.org_a, SERVICE_MANAGER)
    assert constraint_of(excinfo) == "organization_membership_pkey"
    assert ws.grants(ali) == [(ws.org_a, SERVICE_ENGINEER, True)]
    with pytest.raises(prov.ProvisioningConflict):
        prov.grant_access(ws.admin, ali, ws.org_a, SERVICE_MANAGER)


@pytest.mark.db
@pytest.mark.parametrize("role", ["platform_administrator", "owner", ""])
def test_unknown_membership_role_rejected(ws, role):
    import psycopg

    ali = ws.provider()
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        insert_grant(ws.admin, ali, ws.org_a, role)
    assert constraint_of(excinfo) == "organization_membership_role_valid"
    assert ws.grants(ali) == []


@pytest.mark.db
def test_membership_foreign_keys_enforced(ws):
    import psycopg

    ali = ws.provider()
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as excinfo:
        insert_grant(ws.admin, uuid4(), ws.org_a, SERVICE_ENGINEER)
    assert constraint_of(excinfo) == "organization_membership_actor_id_fkey"
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as excinfo:
        insert_grant(ws.admin, ali, uuid4(), SERVICE_ENGINEER)
    assert constraint_of(excinfo) == "organization_membership_organization_id_fkey"
    insert_grant(ws.admin, ali, ws.org_a, SERVICE_ENGINEER)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        ws.admin.execute("DELETE FROM organization WHERE id = %s", (ws.org_a,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        ws.admin.execute("DELETE FROM actor WHERE id = %s", (ali,))
    with pytest.raises(prov.NotFound):
        prov.grant_access(ws.admin, uuid4(), ws.org_a, SERVICE_ENGINEER)
    with pytest.raises(prov.NotFound):
        prov.grant_access(ws.admin, ali, uuid4(), SERVICE_MANAGER)


@pytest.mark.db
def test_catalog_guards_present_and_enabled(ws):
    rows = dict(ws.admin.execute(
        "SELECT tgname, tgenabled FROM pg_trigger WHERE NOT tgisinternal "
        "AND tgrelid IN ('public.actor'::regclass, 'public.organization_membership'::regclass)"
    ).fetchall())
    assert rows == {"actor_grants_remain_eligible": "O",
                    "organization_membership_role_eligible": "O"}
    # The provider is implicit: the migration inserts no rows at all.
    migration = (Path(__file__).resolve().parents[1] / "db"
                 / "003_actor_identity_and_grants.sql").read_text(encoding="utf-8")
    assert "INSERT INTO" not in migration.upper()


# =============================================================================
# Eligibility tests: database guard (direct SQL) and provisioning path
# =============================================================================

@pytest.mark.db
@pytest.mark.parametrize("role", [SERVICE_ENGINEER, SERVICE_MANAGER])
def test_provider_engineer_and_manager_grants_allowed(ws, role):
    direct, provisioned = ws.provider(), ws.provider()
    insert_grant(ws.admin, direct, ws.org_a, role)
    prov.grant_access(ws.admin, provisioned, ws.org_b, role)
    assert ws.grants(direct) == [(ws.org_a, role, True)]
    assert ws.grants(provisioned) == [(ws.org_b, role, True)]


@pytest.mark.db
@pytest.mark.parametrize("active", [True, False])
def test_provider_requester_grant_rejected_even_if_inactive(ws, active):
    import psycopg

    ali = ws.provider()
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        insert_grant(ws.admin, ali, ws.org_a, REQUESTER, active)
    assert constraint_of(excinfo) == "organization_membership_role_eligible"
    with pytest.raises(prov.EligibilityError):
        prov.grant_access(ws.admin, ali, ws.org_a, REQUESTER)
    assert ws.grants(ali) == []


@pytest.mark.db
def test_customer_requester_in_home_organization_allowed(ws):
    ahmed, other = ws.customer(ws.org_a), ws.customer(ws.org_a)
    insert_grant(ws.admin, ahmed, ws.org_a, REQUESTER)
    prov.grant_access(ws.admin, other, ws.org_a, REQUESTER)
    assert ws.grants(ahmed) == [(ws.org_a, REQUESTER, True)]
    assert ws.grants(other) == [(ws.org_a, REQUESTER, True)]


@pytest.mark.db
@pytest.mark.parametrize("active", [True, False])
def test_customer_requester_in_foreign_organization_rejected(ws, active):
    import psycopg

    ahmed = ws.customer(ws.org_a)
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        insert_grant(ws.admin, ahmed, ws.org_b, REQUESTER, active)
    assert constraint_of(excinfo) == "organization_membership_role_eligible"
    with pytest.raises(prov.EligibilityError):
        prov.grant_access(ws.admin, ahmed, ws.org_b, REQUESTER)
    assert ws.grants(ahmed) == []


@pytest.mark.db
@pytest.mark.parametrize("role", [SERVICE_ENGINEER, SERVICE_MANAGER])
@pytest.mark.parametrize("org_key", ["home", "foreign"])
@pytest.mark.parametrize("active", [True, False])
def test_customer_engineer_or_manager_grant_rejected(ws, role, org_key, active):
    import psycopg

    ahmed = ws.customer(ws.org_a)
    org = ws.org_a if org_key == "home" else ws.org_b
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        insert_grant(ws.admin, ahmed, org, role, active)
    assert constraint_of(excinfo) == "organization_membership_role_eligible"
    with pytest.raises(prov.EligibilityError):
        prov.grant_access(ws.admin, ahmed, org, role)
    assert ws.grants(ahmed) == []


@pytest.mark.db
def test_provider_holds_separate_grants_in_two_customers(ws):
    ali = ws.provider()
    prov.grant_access(ws.admin, ali, ws.org_a, SERVICE_ENGINEER)
    prov.grant_access(ws.admin, ali, ws.org_b, SERVICE_ENGINEER)
    assert prov.deactivate_grant(ws.admin, ali, ws.org_b) is True
    assert sorted(ws.grants(ali)) == sorted([
        (ws.org_a, SERVICE_ENGINEER, True), (ws.org_b, SERVICE_ENGINEER, False)])


@pytest.mark.db
def test_update_to_ineligible_role_or_organization_rejected(ws):
    import psycopg

    ahmed, ali = ws.customer(ws.org_a), ws.provider()
    insert_grant(ws.admin, ahmed, ws.org_a, REQUESTER)
    insert_grant(ws.admin, ali, ws.org_a, SERVICE_ENGINEER)
    statements = [
        ("UPDATE organization_membership SET role = 'service_manager' "
         "WHERE actor_id = %s AND organization_id = %s", (ahmed, ws.org_a)),
        ("UPDATE organization_membership SET organization_id = %s "
         "WHERE actor_id = %s AND organization_id = %s", (ws.org_b, ahmed, ws.org_a)),
        ("UPDATE organization_membership SET role = 'requester' "
         "WHERE actor_id = %s AND organization_id = %s", (ali, ws.org_a)),
        # A role change is validated even on an inactive grant.
        ("UPDATE organization_membership SET role = 'service_engineer', is_active = FALSE "
         "WHERE actor_id = %s AND organization_id = %s", (ahmed, ws.org_a)),
    ]
    for sql, params in statements:
        with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
            ws.admin.execute(sql, params)
        assert constraint_of(excinfo) == "organization_membership_role_eligible"
    with pytest.raises(prov.EligibilityError):
        prov.change_grant_role(ws.admin, ahmed, ws.org_a, SERVICE_MANAGER)
    with pytest.raises(prov.EligibilityError):
        prov.change_grant_role(ws.admin, ali, ws.org_a, REQUESTER)
    assert ws.grants(ahmed) == [(ws.org_a, REQUESTER, True)]
    assert ws.grants(ali) == [(ws.org_a, SERVICE_ENGINEER, True)]
    prov.change_grant_role(ws.admin, ali, ws.org_a, SERVICE_MANAGER)
    assert ws.grants(ali) == [(ws.org_a, SERVICE_MANAGER, True)]


@pytest.mark.db
def test_inactive_historical_grant_remains_but_is_not_authoritative(ws):
    actor = ws.customer(ws.org_a)
    prov.grant_access(ws.admin, actor, ws.org_a, REQUESTER)
    prov.deactivate_grant(ws.admin, actor, ws.org_a)
    prov.change_affiliation(ws.admin, actor, CUSTOMER, ws.org_b)
    assert ws.actor(actor) == (CUSTOMER, ws.org_b, True)
    # The historical row is kept, inactive, and is not an active grant.
    assert ws.grants(actor) == [(ws.org_a, REQUESTER, False)]
    assert ws.admin.execute(
        "SELECT count(*) FROM organization_membership WHERE actor_id = %s AND is_active",
        (actor,)).fetchone()[0] == 0
    assert ws.violations() == []


@pytest.mark.db
def test_reactivation_of_incompatible_grant_rejected(ws):
    import psycopg

    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
    prov.deactivate_grant(ws.admin, actor, ws.org_a)
    prov.change_affiliation(ws.admin, actor, CUSTOMER, ws.org_a)
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        ws.admin.execute(
            "UPDATE organization_membership SET is_active = TRUE "
            "WHERE actor_id = %s AND organization_id = %s", (actor, ws.org_a))
    assert constraint_of(excinfo) == "organization_membership_role_eligible"
    with pytest.raises(prov.EligibilityError):
        prov.reactivate_grant(ws.admin, actor, ws.org_a)
    assert ws.grants(actor) == [(ws.org_a, SERVICE_ENGINEER, False)]


@pytest.mark.db
def test_reactivation_of_compatible_grant_allowed(ws):
    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_MANAGER)
    assert prov.deactivate_grant(ws.admin, actor, ws.org_a) is True
    assert prov.deactivate_grant(ws.admin, actor, ws.org_a) is False
    assert prov.reactivate_grant(ws.admin, actor, ws.org_a) is True
    assert prov.reactivate_grant(ws.admin, actor, ws.org_a) is False
    assert ws.grants(actor) == [(ws.org_a, SERVICE_MANAGER, True)]


@pytest.mark.db
@pytest.mark.parametrize("target", ["customer_a", "customer_b"])
def test_affiliation_change_with_incompatible_active_grants_rejected(ws, target):
    import psycopg

    ali = ws.provider()
    prov.grant_access(ws.admin, ali, ws.org_a, SERVICE_ENGINEER)
    home = ws.org_a if target == "customer_a" else ws.org_b
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        ws.admin.execute(
            "UPDATE actor SET affiliation_type = 'customer', home_organization_id = %s "
            "WHERE id = %s", (home, ali))
    assert constraint_of(excinfo) == "actor_grants_remain_eligible"
    with pytest.raises(prov.EligibilityError):
        prov.change_affiliation(ws.admin, ali, CUSTOMER, home)
    assert ws.actor(ali) == (PROVIDER, None, True)
    assert ws.grants(ali) == [(ws.org_a, SERVICE_ENGINEER, True)]


@pytest.mark.db
def test_customer_home_change_with_active_requester_grant_rejected(ws):
    import psycopg

    ahmed = ws.customer(ws.org_a)
    prov.grant_access(ws.admin, ahmed, ws.org_a, REQUESTER)
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        ws.admin.execute("UPDATE actor SET home_organization_id = %s WHERE id = %s",
                         (ws.org_b, ahmed))
    assert constraint_of(excinfo) == "actor_grants_remain_eligible"
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        ws.admin.execute("UPDATE actor SET affiliation_type = 'provider', "
                         "home_organization_id = NULL WHERE id = %s", (ahmed,))
    assert constraint_of(excinfo) == "actor_grants_remain_eligible"
    assert ws.actor(ahmed) == (CUSTOMER, ws.org_a, True)


@pytest.mark.db
def test_affiliation_change_after_deactivating_incompatible_grants_allowed(ws):
    ali, sara = ws.provider(), ws.provider()
    for actor in (ali, sara):
        prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
        prov.grant_access(ws.admin, actor, ws.org_b, SERVICE_ENGINEER)

    # Explicit, separate deactivations, then the change.
    prov.deactivate_grant(ws.admin, ali, ws.org_a)
    prov.deactivate_grant(ws.admin, ali, ws.org_b)
    assert prov.change_affiliation(ws.admin, ali, CUSTOMER, ws.org_a) == []
    assert ws.actor(ali) == (CUSTOMER, ws.org_a, True)

    # Operator-requested reconciliation in one transaction, in organization order.
    deactivated = prov.change_affiliation(ws.admin, sara, CUSTOMER, ws.org_b,
                                          deactivate_incompatible=True)
    assert deactivated == sorted([ws.org_a, ws.org_b])
    assert ws.actor(sara) == (CUSTOMER, ws.org_b, True)
    assert all(not active for _, _, active in ws.grants(sara))
    # The historical grants were deactivated, not deleted or rewritten.
    assert sorted(ws.grants(sara)) == sorted([
        (ws.org_a, SERVICE_ENGINEER, False), (ws.org_b, SERVICE_ENGINEER, False)])
    assert ws.violations() == []


@pytest.mark.db
def test_deactivation_is_never_blocked_and_inactive_role_change_preserves_state(ws):
    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
    prov.deactivate_grant(ws.admin, actor, ws.org_a)
    prov.change_grant_role(ws.admin, actor, ws.org_a, SERVICE_MANAGER)
    assert ws.grants(actor) == [(ws.org_a, SERVICE_MANAGER, False)]


@pytest.mark.db
def test_bypassed_triggers_are_detected_by_integrity_view(ws):
    """Documents the bypass limitation and the restored-data integrity check."""
    import psycopg

    if not ws.admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                            ).fetchone()[0]:
        pytest.skip("Requires a superuser test session to simulate a trigger bypass")
    ahmed = ws.customer(ws.org_a)
    with ws.admin.transaction():
        ws.admin.execute("SET LOCAL session_replication_role = replica")
        insert_grant(ws.admin, ahmed, ws.org_a, SERVICE_MANAGER)  # triggers bypassed
    assert ws.violations() == [(ahmed, ws.org_a, SERVICE_MANAGER, CUSTOMER, ws.org_a)]
    # Reactivating or re-asserting it through normal writes is rejected...
    with pytest.raises(psycopg.errors.CheckViolation):
        ws.admin.execute("UPDATE organization_membership SET is_active = TRUE "
                         "WHERE actor_id = %s", (ahmed,))
    # ...but it can always be revoked.
    assert prov.deactivate_grant(ws.admin, ahmed, ws.org_a) is True
    assert ws.violations() == []


@pytest.mark.db
def test_null_role_reported_by_not_null_constraint(ws):
    import psycopg

    ali = ws.provider()
    with pytest.raises(psycopg.errors.NotNullViolation):
        insert_grant(ws.admin, ali, ws.org_a, None)


@pytest.mark.db
@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_guarded_writes_require_read_committed(ws, level):
    """The actor-side guard would see a stale snapshot above READ COMMITTED."""
    import psycopg

    ali = ws.provider()
    prov.grant_access(ws.admin, ali, ws.org_a, SERVICE_ENGINEER)
    session = ws.conn()
    statements = [
        ("UPDATE actor SET affiliation_type = 'customer', home_organization_id = %s "
         "WHERE id = %s", (ws.org_b, ali)),
        ("INSERT INTO organization_membership (actor_id, organization_id, role) "
         "VALUES (%s, %s, 'service_engineer')", (ali, ws.org_b)),
        ("UPDATE organization_membership SET role = 'service_manager' "
         "WHERE actor_id = %s AND organization_id = %s", (ali, ws.org_a)),
    ]
    for sql, params in statements:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            with session.transaction():
                session.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
                session.execute(sql, params)
    # Revocation is never blocked, whatever the isolation level.
    with session.transaction():
        session.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
        session.execute("UPDATE organization_membership SET is_active = FALSE "
                        "WHERE actor_id = %s AND organization_id = %s", (ali, ws.org_a))
    assert ws.actor(ali) == (PROVIDER, None, True)
    assert ws.grants(ali) == [(ws.org_a, SERVICE_ENGINEER, False)]


@pytest.mark.db
def test_provisioning_refuses_caller_managed_transaction(ws):
    ali = ws.provider()
    session = ws.conn()
    with session.transaction():
        with pytest.raises(prov.ProvisioningError, match="caller-managed transaction"):
            prov.grant_access(session, ali, ws.org_a, SERVICE_ENGINEER)
    with pytest.raises(prov.ProvisioningError, match="inside a transaction"):
        prov.lock_actor(session, ali)
    assert ws.grants(ali) == []


@pytest.mark.db
def test_provisioning_transaction_groups_operations_atomically(ws):
    ali = ws.provider()
    session = ws.conn()
    with pytest.raises(RuntimeError):
        with prov.provisioning_transaction(session):
            prov.grant_access(session, ali, ws.org_a, SERVICE_ENGINEER)
            prov.grant_access(session, ali, ws.org_b, SERVICE_MANAGER)
            raise RuntimeError("synthetic failure before commit")
    assert ws.grants(ali) == []
    with prov.provisioning_transaction(session):
        prov.grant_access(session, ali, ws.org_a, SERVICE_ENGINEER)
        with pytest.raises(prov.ProvisioningError, match="cannot be nested"):
            with prov.provisioning_transaction(session):
                pass
        with pytest.raises(prov.EligibilityError):  # rolled back to its savepoint
            prov.grant_access(session, ali, ws.org_b, REQUESTER)
        prov.grant_access(session, ali, ws.org_b, SERVICE_MANAGER)
    assert sorted(ws.grants(ali)) == sorted([(ws.org_a, SERVICE_ENGINEER, True),
                                             (ws.org_b, SERVICE_MANAGER, True)])


# =============================================================================
# Synthetic scenario provisioning
# =============================================================================

@pytest.mark.db
def test_synthetic_scenario_provisioning_is_correct_and_rerunnable(ws):
    suffix = f" [test {uuid4().hex[:8]}]"
    kwargs = dict(org_a_slug=ws.slug(ws.org_a), org_b_slug=ws.slug(ws.org_b), label_suffix=suffix)
    first = prov.provision_synthetic_scenario(ws.admin, **kwargs)
    ws.actor_ids.extend(first.actor_ids.values())
    ids = first.actor_ids
    assert set(first.created_actors) == {"ahmed", "fatima", "ali", "sara", "bilal"}
    assert len(first.created_grants) == 6

    a, b = ws.org_a, ws.org_b
    assert ws.actor(ids["ahmed"]) == (CUSTOMER, a, True)
    assert ws.actor(ids["fatima"]) == (CUSTOMER, b, True)
    for key in ("ali", "sara", "bilal"):
        assert ws.actor(ids[key]) == (PROVIDER, None, True)
    assert ws.grants(ids["ahmed"]) == [(a, REQUESTER, True)]
    assert ws.grants(ids["fatima"]) == [(b, REQUESTER, True)]
    assert sorted(ws.grants(ids["ali"])) == sorted([(a, SERVICE_ENGINEER, True),
                                                    (b, SERVICE_ENGINEER, True)])
    assert sorted(ws.grants(ids["sara"])) == sorted([(a, SERVICE_MANAGER, True),
                                                     (b, SERVICE_MANAGER, True)])
    assert ws.grants(ids["bilal"]) == []
    assert ws.violations() == []

    second = prov.provision_synthetic_scenario(ws.admin, **kwargs)
    assert second.actor_ids == ids
    assert second.created_actors == () and second.created_grants == ()

    # A drifted grant is reported, never silently reactivated.
    prov.deactivate_grant(ws.admin, ids["ali"], b)
    with pytest.raises(prov.ProvisioningConflict):
        prov.provision_synthetic_scenario(ws.admin, **kwargs)
    assert (b, SERVICE_ENGINEER, False) in ws.grants(ids["ali"])


# =============================================================================
# Concurrency tests
# =============================================================================

@pytest.mark.db
def test_provisioned_grant_waits_for_affiliation_change_then_is_rejected(ws, pool):
    actor = ws.provider()
    holder, waiter = ws.conn(), ws.conn()
    with prov.provisioning_transaction(holder):
        prov.change_affiliation(holder, actor, CUSTOMER, ws.org_a)  # uncommitted, holds lock
        fut = pool.submit(prov.grant_access, waiter, actor, ws.org_b, SERVICE_ENGINEER)
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "EligibilityError"
    assert ws.actor(actor) == (CUSTOMER, ws.org_a, True)
    assert ws.grants(actor) == []
    assert ws.violations() == []


@pytest.mark.db
def test_provisioned_affiliation_change_waits_for_grant_then_is_rejected(ws, pool):
    actor = ws.provider()
    holder, waiter = ws.conn(), ws.conn()
    with prov.provisioning_transaction(holder):
        prov.grant_access(holder, actor, ws.org_a, SERVICE_ENGINEER)
        fut = pool.submit(prov.change_affiliation, waiter, actor, CUSTOMER, ws.org_a)
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "EligibilityError"
    assert ws.actor(actor) == (PROVIDER, None, True)
    assert ws.grants(actor) == [(ws.org_a, SERVICE_ENGINEER, True)]
    assert ws.violations() == []


@pytest.mark.db
def test_direct_grant_insert_waits_in_trigger_for_affiliation_change(ws, pool):
    actor = ws.provider()
    holder, waiter = ws.conn(), ws.conn()
    with holder.transaction():
        holder.execute("UPDATE actor SET affiliation_type = 'customer', "
                       "home_organization_id = %s WHERE id = %s", (ws.org_a, actor))
        fut = pool.submit(insert_grant, waiter, actor, ws.org_b, SERVICE_ENGINEER)
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "organization_membership_role_eligible"
    assert ws.actor(actor) == (CUSTOMER, ws.org_a, True)
    assert ws.grants(actor) == []


@pytest.mark.db
def test_direct_affiliation_change_waits_for_uncommitted_grant_then_is_rejected(ws, pool):
    """The actor-side guard sees the grant only after the trigger's FOR SHARE wait."""
    actor = ws.provider()
    holder, waiter = ws.conn(), ws.conn()
    with holder.transaction():
        insert_grant(holder, actor, ws.org_a, SERVICE_ENGINEER)
        fut = pool.submit(waiter.execute,
                          "UPDATE actor SET affiliation_type = 'customer', "
                          "home_organization_id = %s WHERE id = %s", (ws.org_a, actor))
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "actor_grants_remain_eligible"
    assert ws.actor(actor) == (PROVIDER, None, True)
    assert ws.grants(actor) == [(ws.org_a, SERVICE_ENGINEER, True)]


@pytest.mark.db
def test_provisioned_reactivation_waits_for_affiliation_change_then_is_rejected(ws, pool):
    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
    prov.deactivate_grant(ws.admin, actor, ws.org_a)
    holder, waiter = ws.conn(), ws.conn()
    with prov.provisioning_transaction(holder):
        prov.change_affiliation(holder, actor, CUSTOMER, ws.org_a)
        fut = pool.submit(prov.reactivate_grant, waiter, actor, ws.org_a)
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "EligibilityError"
    assert ws.grants(actor) == [(ws.org_a, SERVICE_ENGINEER, False)]
    assert ws.violations() == []


@pytest.mark.db
def test_direct_reactivation_waits_in_trigger_for_affiliation_change(ws, pool):
    actor = ws.provider()
    insert_grant(ws.admin, actor, ws.org_a, SERVICE_ENGINEER, active=False)
    holder, waiter = ws.conn(), ws.conn()
    with holder.transaction():
        holder.execute("UPDATE actor SET affiliation_type = 'customer', "
                       "home_organization_id = %s WHERE id = %s", (ws.org_a, actor))
        fut = pool.submit(waiter.execute,
                          "UPDATE organization_membership SET is_active = TRUE "
                          "WHERE actor_id = %s AND organization_id = %s", (actor, ws.org_a))
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "organization_membership_role_eligible"
    assert ws.grants(actor) == [(ws.org_a, SERVICE_ENGINEER, False)]


@pytest.mark.db
def test_direct_affiliation_change_waits_for_uncommitted_reactivation(ws, pool):
    actor = ws.provider()
    insert_grant(ws.admin, actor, ws.org_a, SERVICE_ENGINEER, active=False)
    holder, waiter = ws.conn(), ws.conn()
    with holder.transaction():
        holder.execute("UPDATE organization_membership SET is_active = TRUE "
                       "WHERE actor_id = %s AND organization_id = %s", (actor, ws.org_a))
        fut = pool.submit(waiter.execute,
                          "UPDATE actor SET affiliation_type = 'customer', "
                          "home_organization_id = %s WHERE id = %s", (ws.org_a, actor))
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "actor_grants_remain_eligible"
    assert ws.actor(actor) == (PROVIDER, None, True)
    assert ws.grants(actor) == [(ws.org_a, SERVICE_ENGINEER, True)]


@pytest.mark.db
def test_role_update_waits_for_deactivation_and_preserves_it(ws, pool):
    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
    holder, waiter = ws.conn(), ws.conn()
    with prov.provisioning_transaction(holder):
        prov.deactivate_grant(holder, actor, ws.org_a)
        fut = pool.submit(prov.change_grant_role, waiter, actor, ws.org_a, SERVICE_MANAGER)
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "ok"
    # No lost update: the role change did not resurrect the revoked grant.
    assert ws.grants(actor) == [(ws.org_a, SERVICE_MANAGER, False)]


@pytest.mark.db
def test_revocation_waits_for_in_flight_request_time_locks(ws, pool):
    """ADR-0005 request-time order: actor FOR SHARE, then membership FOR SHARE."""
    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
    holder, waiter = ws.conn(), ws.conn()
    with holder.transaction():
        holder.execute("SELECT 1 FROM actor WHERE id = %s AND is_active FOR SHARE", (actor,))
        holder.execute("SELECT role FROM organization_membership WHERE actor_id = %s "
                       "AND organization_id = %s AND is_active FOR SHARE", (actor, ws.org_a))
        fut = pool.submit(prov.deactivate_grant, waiter, actor, ws.org_a)
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
    assert outcome(fut) == "ok"
    assert ws.grants(actor) == [(ws.org_a, SERVICE_ENGINEER, False)]


@pytest.mark.db
def test_provisioning_locks_actor_before_membership_row(ws, pool):
    import psycopg

    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
    holder, waiter, probe = ws.conn(), ws.conn(), ws.conn()
    with holder.transaction():
        holder.execute("SELECT 1 FROM actor WHERE id = %s FOR SHARE", (actor,))
        fut = pool.submit(prov.deactivate_grant, waiter, actor, ws.org_a)
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
        # While provisioning waits for the actor, it holds no membership row lock.
        with probe.transaction():
            probe.execute("SELECT 1 FROM organization_membership WHERE actor_id = %s "
                          "AND organization_id = %s FOR UPDATE NOWAIT", (actor, ws.org_a))
    assert outcome(fut) == "ok"

    # Contrast: a direct UPDATE locks the membership row BEFORE its trigger
    # waits for the actor, which is the inversion ADR-0006 warns about.
    with holder.transaction():
        prov.lock_actor(holder, actor)
        fut = pool.submit(waiter.execute,
                          "UPDATE organization_membership SET role = 'service_manager' "
                          "WHERE actor_id = %s AND organization_id = %s", (actor, ws.org_a))
        wait_until_blocked(ws.admin, pid(waiter), pid(holder), fut)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with probe.transaction():
                probe.execute("SELECT 1 FROM organization_membership WHERE actor_id = %s "
                              "AND organization_id = %s FOR UPDATE NOWAIT", (actor, ws.org_a))
    assert outcome(fut) == "ok"
    assert ws.grants(actor) == [(ws.org_a, SERVICE_MANAGER, False)]


@pytest.mark.db
def test_for_key_share_does_not_block_affiliation_change_but_for_share_does(ws):
    """Why the membership guard locks FOR SHARE rather than relying on the FK lock."""
    import psycopg

    actor = ws.provider()
    holder, writer = ws.conn(), ws.conn()
    writer.execute("SET lock_timeout = '500ms'")
    with holder.transaction():
        holder.execute("SELECT 1 FROM actor WHERE id = %s FOR KEY SHARE", (actor,))
        writer.execute("UPDATE actor SET affiliation_type = 'customer', "
                       "home_organization_id = %s WHERE id = %s", (ws.org_a, actor))
    assert ws.actor(actor) == (CUSTOMER, ws.org_a, True)
    with holder.transaction():
        holder.execute("SELECT 1 FROM actor WHERE id = %s FOR SHARE", (actor,))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            writer.execute("UPDATE actor SET home_organization_id = %s WHERE id = %s",
                           (ws.org_b, actor))
    assert ws.actor(actor) == (CUSTOMER, ws.org_a, True)


@pytest.mark.db
def test_deadlock_from_protocol_bypass_aborts_one_side_and_is_not_success(ws, pool):
    """A direct writer that bypasses actor-first locking can deadlock provisioning."""
    if ws.admin.execute("SELECT EXTRACT(epoch FROM current_setting('deadlock_timeout')::interval)"
                        ).fetchone()[0] >= 4:
        pytest.skip("deadlock_timeout must be below the 5 s provisioning lock timeout")
    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
    provisioner, direct = ws.conn(), ws.conn()

    direct_fut = None
    prov_result = None
    with prov.provisioning_transaction(provisioner):
        prov.lock_actor(provisioner, actor)                      # 1. actor first
        direct_fut = pool.submit(                                 # 2. row lock, then trigger
            direct.execute,                                       #    waits for the actor
            "UPDATE organization_membership SET role = 'service_manager' "
            "WHERE actor_id = %s AND organization_id = %s", (actor, ws.org_a))
        wait_until_blocked(ws.admin, pid(direct), pid(provisioner), direct_fut)
        try:                                                      # 3. provisioner needs the row
            prov.deactivate_grant(provisioner, actor, ws.org_a)
            prov_result = "ok"
        except prov.ProvisioningError as exc:
            prov_result = str(exc)
    direct_result = outcome(direct_fut)

    results = {"provisioning": prov_result, "direct": direct_result}
    deadlocks = [k for k, v in results.items() if "eadlock" in v]
    assert len(deadlocks) == 1, results
    print(f"\ndeadlock outcome: {results}")
    grants = ws.grants(actor)
    if deadlocks == ["direct"]:
        assert prov_result == "ok"
        assert grants == [(ws.org_a, SERVICE_ENGINEER, False)]
    else:
        assert prov_result.startswith("Deadlock detected")  # reported as failure
        assert direct_result == "ok"
        assert grants == [(ws.org_a, SERVICE_MANAGER, True)]
    assert ws.violations() == []


@pytest.mark.db
def test_deadlock_victim_provisioning_reports_failure_and_writes_nothing(ws, pool):
    """Force provisioning to be the deadlock victim (superuser-only setting)."""
    if not ws.admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                            ).fetchone()[0]:
        pytest.skip("Requires a superuser test session to raise deadlock_timeout")
    actor = ws.provider()
    prov.grant_access(ws.admin, actor, ws.org_a, SERVICE_ENGINEER)
    provisioner, direct = ws.conn(), ws.conn()
    direct.execute("SET deadlock_timeout = '8s'")  # provisioner's 1s check fires first

    with prov.provisioning_transaction(provisioner):
        prov.lock_actor(provisioner, actor)
        direct_fut = pool.submit(
            direct.execute,
            "UPDATE organization_membership SET role = 'service_manager' "
            "WHERE actor_id = %s AND organization_id = %s", (actor, ws.org_a))
        wait_until_blocked(ws.admin, pid(direct), pid(provisioner), direct_fut)
        with pytest.raises(prov.ProvisioningError, match="^Deadlock detected"):
            prov.deactivate_grant(provisioner, actor, ws.org_a)
        # The failed deactivation left nothing behind inside this transaction.
        assert provisioner.execute(
            "SELECT is_active FROM organization_membership WHERE actor_id = %s",
            (actor,)).fetchone()[0] is True
    assert outcome(direct_fut) == "ok"
    assert ws.grants(actor) == [(ws.org_a, SERVICE_MANAGER, True)]
    assert ws.violations() == []


@pytest.mark.db
def test_lock_timeout_is_reported_as_failure_without_writes(ws):
    actor = ws.provider()
    holder, waiter = ws.conn(), ws.conn()
    with holder.transaction():
        holder.execute("SELECT 1 FROM actor WHERE id = %s FOR SHARE", (actor,))
        started = time.monotonic()
        with pytest.raises(prov.ProvisioningError, match="Lock timeout"):
            prov.grant_access(waiter, actor, ws.org_a, SERVICE_ENGINEER, lock_timeout_ms=300)
        elapsed = time.monotonic() - started
    assert 0.25 <= elapsed < 5
    assert ws.grants(actor) == []


RACE_ITERATIONS = 25


@pytest.mark.db
@pytest.mark.parametrize("path", ["provisioning", "direct_sql"])
def test_racing_grant_and_affiliation_change_never_both_commit(ws, pool, path):
    counts = {}
    for _ in range(RACE_ITERATIONS):
        actor = ws.provider()
        c1, c2 = ws.conn(), ws.conn()
        barrier = Barrier(2)

        if path == "provisioning":
            def grant():
                barrier.wait(timeout=WAIT_SECONDS)
                time.sleep(random.uniform(0, 0.004))
                prov.grant_access(c1, actor, ws.org_b, SERVICE_ENGINEER)

            def change():
                barrier.wait(timeout=WAIT_SECONDS)
                time.sleep(random.uniform(0, 0.004))
                prov.change_affiliation(c2, actor, CUSTOMER, ws.org_a)
        else:
            def grant():
                barrier.wait(timeout=WAIT_SECONDS)
                time.sleep(random.uniform(0, 0.004))
                insert_grant(c1, actor, ws.org_b, SERVICE_ENGINEER)

            def change():
                barrier.wait(timeout=WAIT_SECONDS)
                time.sleep(random.uniform(0, 0.004))
                c2.execute("UPDATE actor SET affiliation_type = 'customer', "
                           "home_organization_id = %s WHERE id = %s", (ws.org_a, actor))

        f1, f2 = pool.submit(grant), pool.submit(change)
        pair = (outcome(f1), outcome(f2))
        counts[pair] = counts.get(pair, 0) + 1
        assert pair.count("ok") == 1, pair
        affiliation = ws.actor(actor)[0]
        grants = ws.grants(actor)
        if pair[0] == "ok":
            assert affiliation == PROVIDER and grants == [(ws.org_b, SERVICE_ENGINEER, True)]
        else:
            assert affiliation == CUSTOMER and grants == []
        for c in (c1, c2):
            c.close()
    assert ws.violations() == []
    # Visible with -s / -rA for the evidence report.
    print(f"\n{path} race outcomes over {RACE_ITERATIONS} iterations: {counts}")
