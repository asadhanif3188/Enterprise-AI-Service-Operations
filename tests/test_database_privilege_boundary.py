"""Database-privilege assessment for the authorized case operations (investigation only).

Question: can the authorized Python operations run under a non-superuser
PostgreSQL role with only read and case-create privileges, and what does that
role still allow? This is evidence for a future database-role decision. It is
NOT a production role, a migration, or row-level security, and it does not
claim PostgreSQL-enforced tenant isolation.

Opt-in: roles are cluster-wide objects, so these tests run only when
EAISO_ALLOW_TEST_ROLES=1 is set, against a disposable PostgreSQL cluster whose
TEST_DATABASE_URL user may create roles. Each test creates one uniquely named
LOGIN role with a random password generated at run time (never stored), and
drops it afterwards. Without the opt-in they are reported as skipped.
"""

# Fixtures are imported from the centralized-authorization suite and used as
# test parameters, which ruff reports as unused imports and redefinitions.
# ruff: noqa: F401, F811

from __future__ import annotations

import os
import secrets
from uuid import uuid4

import pytest
from test_case_authorization import authz_dsn, outcome, world

from src import credential_auth as auth
from src.authorized_case_operations import AuthorizedCaseOperations
from src.case_authorization import CaseNotFound, CaseOperationUnavailable

BASE_GRANTS = (
    "GRANT USAGE ON SCHEMA public TO {role}",
    "GRANT SELECT ON actor, organization_membership, case_record TO {role}",
    "GRANT INSERT ON case_record TO {role}",
)
# PostgreSQL requires UPDATE privilege on at least one column for SELECT ... FOR
# SHARE. created_at is the least consequential column to expose.
LOCK_GRANTS = (
    "GRANT UPDATE (created_at) ON actor TO {role}",
    "GRANT UPDATE (created_at) ON organization_membership TO {role}",
)


class RestrictedRole:
    def __init__(self, world, grants):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        self.world = world
        self.name = f"eaiso_test_restricted_{uuid4().hex[:12]}"
        password = secrets.token_urlsafe(24)
        admin = world.admin
        self.database = admin.execute("SELECT current_database()").fetchone()[0]  # eaiso_test (guarded)
        self.conn = None
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                              "NOREPLICATION NOBYPASSRLS PASSWORD {}").format(
            sql.Identifier(self.name), sql.Literal(password)))
        try:
            admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(self.database), sql.Identifier(self.name)))
            for statement in grants:
                admin.execute(sql.SQL(statement).format(role=sql.Identifier(self.name)))
            self.dsn = make_conninfo(world.dsn, user=self.name, password=password)
            self.conn = psycopg.connect(self.dsn, autocommit=True)
        except Exception:
            self.drop()  # never leave a cluster-wide role behind after a partial setup
            raise
        resolver = auth.CredentialResolver(auth.FileCredentialStore(world.store),
                                           auth.PostgresActorLookup(self.dsn))
        self.ops = AuthorizedCaseOperations(resolver, self.dsn)

    def attempt(self, statement, params=None):
        """Run one statement as the restricted role; return the error class name or 'allowed'."""
        import psycopg

        try:
            self.conn.execute(statement, params)
        except psycopg.Error as exc:
            return type(exc).__name__
        return "allowed"

    def drop(self):
        from psycopg import sql

        if self.conn is not None:
            self.conn.close()
        admin = self.world.admin
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
                      (self.name,))
        admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(self.name)))
        admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
            sql.Identifier(self.database), sql.Identifier(self.name)))
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(self.name)))


@pytest.fixture
def make_role(world):
    if os.getenv("EAISO_ALLOW_TEST_ROLES") != "1":
        pytest.skip("Set EAISO_ALLOW_TEST_ROLES=1 on a disposable cluster; privilege cases not executed")
    can_create = world.admin.execute(
        "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone()[0]
    if not can_create:
        pytest.skip("TEST_DATABASE_URL user cannot create roles; privilege cases not executed")
    created = []

    def factory(grants):
        role = RestrictedRole(world, grants)
        created.append(role)
        return role

    try:
        yield factory
    finally:
        for role in created:
            role.drop()


def identity_state(world):
    ids = list(world.actors.values())
    actors = world.admin.execute(
        "SELECT id, is_active, affiliation_type, home_organization_id FROM actor "
        "WHERE id = ANY(%s) ORDER BY id", (ids,)).fetchall()
    grants = world.admin.execute(
        "SELECT actor_id, organization_id, role, is_active FROM organization_membership "
        "WHERE actor_id = ANY(%s) ORDER BY actor_id, organization_id", (ids,)).fetchall()
    return actors, grants


@pytest.mark.db
def test_read_and_insert_privileges_alone_cannot_take_the_create_locks(world, make_role):
    """Finding: FOR SHARE needs UPDATE privilege on a column; without it create fails closed."""
    role = make_role(BASE_GRANTS)
    cred = world.creds["engineer_a"]
    a_case = world.seed_case(world.org_a)
    before = world.snapshot()
    result = outcome(role.ops.create_case, cred, str(world.org_a), "x", "y")
    assert isinstance(result, CaseOperationUnavailable) and result.reason == "database"
    assert world.snapshot() == before  # fail closed: no write, not reported as a denial
    # Reads and lists take no row locks and work with SELECT alone.
    assert role.ops.get_case(cred, str(a_case))["id"] == a_case
    assert [i["id"] for i in role.ops.list_cases(cred, str(world.org_a))["items"]] == [a_case]


@pytest.mark.db
def test_restricted_role_runs_the_authorized_operations(world, make_role):
    role = make_role(BASE_GRANTS + LOCK_GRANTS)
    assert world.admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = %s",
                               (role.name,)).fetchone() == (False,)
    cred = world.creds["engineer_a"]
    created = role.ops.create_case(cred, str(world.org_a), "Synthetic restricted-role case", "Fictional")
    assert created["organization_id"] == world.org_a
    assert role.ops.get_case(cred, str(created["id"]))["id"] == created["id"]
    assert [i["id"] for i in role.ops.list_cases(cred, str(world.org_a))["items"]] == [created["id"]]
    # Application-layer isolation still applies under this role.
    b_case = world.seed_case(world.org_b)
    assert isinstance(outcome(role.ops.get_case, cred, str(b_case)), CaseNotFound)
    persisted = world.admin.execute("SELECT organization_id FROM case_record WHERE id = %s",
                                    (created["id"],)).fetchone()
    assert persisted == (world.org_a,)


@pytest.mark.db
def test_restricted_role_cannot_change_identity_grants_or_bypass_guards(world, make_role):
    role = make_role(BASE_GRANTS + LOCK_GRANTS)
    me, a, b = world.actors["engineer_a"], world.org_a, world.org_b
    a_case = world.seed_case(a)
    identity_before, cases_before = identity_state(world), world.snapshot()
    attempts = {
        "change own affiliation": (("UPDATE actor SET affiliation_type = 'customer', "
                                    "home_organization_id = %s WHERE id = %s"), (a, me)),
        "deactivate an actor": ("UPDATE actor SET is_active = FALSE WHERE id = %s", (me,)),
        "create an actor": (("INSERT INTO actor (display_name, affiliation_type) "
                             "VALUES ('synthetic intruder', 'provider')"), None),
        "delete an actor": ("DELETE FROM actor WHERE id = %s", (me,)),
        "grant itself customer B": (("INSERT INTO organization_membership (actor_id, organization_id, role) "
                                     "VALUES (%s, %s, 'service_engineer')"), (me, b)),
        "change a grant role": (("UPDATE organization_membership SET role = 'service_manager' "
                                 "WHERE actor_id = %s"), (me,)),
        "reactivate or deactivate a grant": (("UPDATE organization_membership SET is_active = FALSE "
                                              "WHERE actor_id = %s"), (me,)),
        "delete a grant": ("DELETE FROM organization_membership WHERE actor_id = %s", (me,)),
        "update a case": ("UPDATE case_record SET status = 'CLOSED' WHERE id = %s", (a_case,)),
        "delete a case": ("DELETE FROM case_record WHERE id = %s", (a_case,)),
        "create an organization": ("INSERT INTO organization (name, slug) VALUES ('x', %s)",
                                   (f"intruder-{uuid4().hex}",)),
        "disable grant triggers": ("ALTER TABLE organization_membership DISABLE TRIGGER ALL", None),
        "skip triggers via replication role": ("SET session_replication_role = replica", None),
        "read the eligibility view": ("SELECT * FROM organization_membership_eligibility_violation", None),
    }
    results = {label: role.attempt(sql_text, params) for label, (sql_text, params) in attempts.items()}
    assert results == {label: "InsufficientPrivilege" for label in attempts}, results
    assert identity_state(world) == identity_before
    assert world.snapshot() == cases_before


@pytest.mark.db
def test_database_privileges_alone_do_not_isolate_customers(world, make_role):
    """Characterization of the limitation, not a passing security property.

    With SELECT on case_record (required by the authorized reads), the same
    role can read every customer's cases by direct SQL. Tenant isolation is
    enforced only by the application layer; there is no row-level security.
    The column UPDATE grant needed for FOR SHARE is also a (narrow) write and
    row-locking capability.
    """
    role = make_role(BASE_GRANTS + LOCK_GRANTS)
    me = world.actors["engineer_a"]                 # holds a grant only in customer A
    b_case = world.seed_case(world.org_b, title="Synthetic customer-B case")
    rows = role.conn.execute("SELECT id, organization_id, title, description FROM case_record "
                             "WHERE organization_id = %s", (world.org_b,)).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(b_case, world.org_b, "Synthetic customer-B case")]
    assert role.attempt("SELECT 1 FROM actor WHERE id = %s FOR UPDATE", (me,)) == "allowed"
    assert role.attempt("UPDATE actor SET created_at = created_at WHERE id = %s", (me,)) == "allowed"
