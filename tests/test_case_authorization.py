"""Centralized case authorization (ADR-0005) through AuthorizedCaseOperations.

Offline tests cover the policy and input handling. Tests marked ``db`` require
TEST_DATABASE_URL pointing to the dedicated ``eaiso_test`` database with
db/001-003 applied. Each test builds isolated fictional organizations, actors,
grants, credentials (generated at test time, never committed), and cases, and
removes them afterwards. Concurrency tests coordinate real PostgreSQL sessions
and treat a session as waiting only when pg_blocking_pids() names the holder.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from src import case_authorization as policy
from src import credential_auth as auth
from src.authorized_case_operations import AuthorizedCaseOperations
from src.case_authorization import (
    CaseCreationOutcomeUnknown,
    CaseNotFound,
    CaseOperationUnavailable,
    InvalidCaseRequest,
    OrganizationAccessDenied,
    UnsupportedAction,
)

WAIT_SECONDS = 10


# =============================================================================
# Offline: policy matrix and input handling
# =============================================================================

@pytest.mark.parametrize("role,action,allowed", [
    ("service_engineer", "case:create", True),
    ("service_engineer", "case:read", True),
    ("service_engineer", "case:list", True),
    ("service_manager", "case:create", True),
    ("service_manager", "case:read", True),
    ("service_manager", "case:list", True),
    ("requester", "case:create", True),
    ("requester", "case:read", False),
    ("requester", "case:list", False),
    ("platform_administrator", "case:create", False),
    ("platform_administrator", "case:read", False),
    ("platform_administrator", "case:list", False),
    ("owner", "case:read", False),
    ("", "case:create", False),
    (None, "case:create", False),
    ("service_engineer", "case:update", False),
    ("service_engineer", "case:transition", False),
    ("service_manager", "membership:grant", False),
    ("service_engineer", "", False),
    ("service_engineer", None, False),
])
def test_permission_matrix_offline(role, action, allowed):
    assert policy.permits(role, action) is allowed


def test_policy_recognizes_exactly_three_actions_offline():
    assert policy.ACTIONS == {"case:create", "case:read", "case:list"}
    assert policy.roles_permitting("case:create") == ["requester", "service_engineer", "service_manager"]
    assert policy.roles_permitting("case:read") == ["service_engineer", "service_manager"]
    assert policy.roles_permitting("case:list") == ["service_engineer", "service_manager"]
    for action in ("case:update", "case:delete", "admin", "", None):
        with pytest.raises(UnsupportedAction):
            policy.roles_permitting(action)
        with pytest.raises(UnsupportedAction):
            policy.require_supported_action(action)


class StaticResolver:
    """Test double: maps a test credential to a fixed actor ID."""

    def __init__(self, mapping):
        self.mapping = mapping

    def resolve(self, credential):
        if isinstance(credential, str) and credential in self.mapping:
            return auth.VerifiedActor(self.mapping[credential], "0000000000000000")
        raise auth.Unauthenticated("unknown")


UNREACHABLE_DSN = "postgresql://nobody:unused@127.0.0.1:1/eaiso_test"


@pytest.mark.parametrize("call", [
    lambda ops: ops.create_case("cred", "not-a-uuid", "t", "d"),
    lambda ops: ops.create_case("cred", 123, "t", "d"),
    lambda ops: ops.create_case("cred", str(uuid4()), "  ", "d"),
    lambda ops: ops.create_case("cred", str(uuid4()), "t", None),
    lambda ops: ops.get_case("cred", "../etc"),
    lambda ops: ops.list_cases("cred", "x"),
    lambda ops: ops.list_cases("cred", str(uuid4()), limit=0),
    lambda ops: ops.list_cases("cred", str(uuid4()), limit=101),
    lambda ops: ops.list_cases("cred", str(uuid4()), limit="5"),
    lambda ops: ops.list_cases("cred", str(uuid4()), limit=True),
    lambda ops: ops.create_case("cred", str(uuid4()), "nul\x00title", "d"),
    lambda ops: ops.create_case("cred", str(uuid4()), "t", "x" * 10_001),
    lambda ops: ops.get_case("cred", "{" + str(uuid4()) + "}"),
    lambda ops: ops.get_case("cred", "urn:uuid:" + str(uuid4())),
    lambda ops: ops.get_case("cred", uuid4().hex),
    # Readiness-review regression: text that cannot be encoded as UTF-8 (a lone
    # surrogate) must be rejected before any lock, not escape as a raw
    # UnicodeEncodeError that carries the submitted text.
    lambda ops: ops.create_case("cred", str(uuid4()), "lone " + chr(0xD800) + " surrogate", "d"),
    lambda ops: ops.create_case("cred", str(uuid4()), "t", chr(0xDFFF)),
])
def test_invalid_input_rejected_before_database_offline(call):
    ops = AuthorizedCaseOperations(StaticResolver({"cred": uuid4()}), UNREACHABLE_DSN)
    with pytest.raises(InvalidCaseRequest):
        call(ops)  # an unreachable DSN proves no connection was attempted


def test_authentication_precedes_input_validation_offline():
    ops = AuthorizedCaseOperations(StaticResolver({}), UNREACHABLE_DSN)
    with pytest.raises(auth.Unauthenticated):
        ops.create_case("unknown", "not-a-uuid", "", "")


def test_entry_points_accept_no_actor_or_scope_parameters_offline():
    sigs = {name: list(inspect.signature(getattr(AuthorizedCaseOperations, name)).parameters)
            for name in ("create_case", "get_case", "list_cases")}
    assert sigs == {
        "create_case": ["self", "credential", "organization_id", "title", "description"],
        "get_case": ["self", "credential", "case_id"],
        "list_cases": ["self", "credential", "organization_id", "limit"],
    }


# =============================================================================
# Database fixtures
# =============================================================================

@pytest.fixture
def authz_dsn():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for eaiso_test; database cases not executed")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    if conninfo_to_dict(dsn).get("dbname") != "eaiso_test":
        pytest.fail("Safety guard: TEST_DATABASE_URL must refer to eaiso_test")
    with psycopg.connect(dsn) as conn:
        name, grants = conn.execute(
            "SELECT current_database(), to_regclass('public.organization_membership')").fetchone()
    if name != "eaiso_test":
        pytest.fail("Safety guard: actual connected database is not eaiso_test")
    if grants is None:
        pytest.fail("Apply db/003_actor_identity_and_grants.sql to eaiso_test first")
    return dsn


class World:
    """Fictional customers A and B, actors with grants, credentials, and cases."""

    def __init__(self, dsn, tmp_path):
        from src import identity_provisioning as prov

        self.dsn = dsn
        self.prov = prov
        self.admin = prov.connect(dsn)
        self.conns = []
        self.orgs = [self._org("a"), self._org("b")]
        self.org_a, self.org_b = self.orgs
        self.actors = {}
        self.creds = {}
        self.store = tmp_path / "credentials.json"

        a, b = self.org_a, self.org_b
        self._actor("engineer_a", prov.PROVIDER, None, [(a, prov.SERVICE_ENGINEER)])
        self._actor("manager_a", prov.PROVIDER, None, [(a, prov.SERVICE_MANAGER)])
        self._actor("requester_a", prov.CUSTOMER, a, [(a, prov.REQUESTER)])
        self._actor("engineer_ab", prov.PROVIDER, None,
                    [(a, prov.SERVICE_ENGINEER), (b, prov.SERVICE_ENGINEER)])
        self._actor("engineer_b", prov.PROVIDER, None, [(b, prov.SERVICE_ENGINEER)])
        self._actor("platform_admin", prov.PROVIDER, None, [])
        self.resolver = auth.CredentialResolver(auth.FileCredentialStore(self.store),
                                                auth.PostgresActorLookup(dsn))
        self.ops = self.operations()

    def _org(self, tag):
        return self.admin.execute(
            "INSERT INTO organization (name, slug) VALUES (%s, %s) RETURNING id",
            (f"Fictional authorization-test customer {tag}", f"authz-test-{tag}-{uuid4().hex}"),
        ).fetchone()[0]

    def _actor(self, key, affiliation, home, grants):
        actor_id = self.prov.create_actor(self.admin, f"synthetic {key}", affiliation, home)
        for org, role in grants:
            self.prov.grant_access(self.admin, actor_id, org, role)
        self.actors[key] = actor_id
        self.creds[key], _ = auth.issue_credential(self.store, actor_id)

    def operations(self, dsn=None, **kwargs):
        return AuthorizedCaseOperations(self.resolver, dsn or self.dsn, **kwargs)

    def conn(self):
        import psycopg

        c = psycopg.connect(self.dsn, autocommit=True,
                            options="-c lock_timeout=10s -c statement_timeout=20s")
        self.conns.append(c)
        return c

    def seed_case(self, org, title="Synthetic seeded case"):
        return self.admin.execute(
            "INSERT INTO case_record (organization_id, title, description) VALUES (%s, %s, %s) "
            "RETURNING id", (org, title, "Fictional description for authorization tests"),
        ).fetchone()[0]

    def snapshot(self):
        rows = self.admin.execute(
            "SELECT id, organization_id, title, description, status, version FROM case_record "
            "WHERE organization_id = ANY(%s) ORDER BY id", (self.orgs,)).fetchall()
        total = self.admin.execute("SELECT count(*) FROM case_record").fetchone()[0]
        return rows, total

    def grant(self, key, org):
        return self.admin.execute(
            "SELECT role, is_active FROM organization_membership WHERE actor_id = %s "
            "AND organization_id = %s", (self.actors[key], org)).fetchone()

    def close(self):
        import psycopg

        for c in self.conns:
            try:
                c.close()
            except Exception:
                pass
        self.admin.close()
        ids = list(self.actors.values())
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("DELETE FROM case_record WHERE organization_id = ANY(%s)", (self.orgs,))
            c.execute("DELETE FROM organization_membership WHERE actor_id = ANY(%s)", (ids,))
            c.execute("DELETE FROM actor WHERE id = ANY(%s)", (ids,))
            c.execute("DELETE FROM organization WHERE id = ANY(%s)", (self.orgs,))


@pytest.fixture
def world(authz_dsn, tmp_path):
    w = World(authz_dsn, tmp_path)
    try:
        yield w
    finally:
        w.close()


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(max_workers=4)
    try:
        yield executor
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def outcome(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - classification for assertions
        return exc


def wait_blocked_by(monitor, holder_pid, query_fragment, future=None):
    """Return the pid of a session blocked by holder_pid whose query contains the fragment."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if future is not None and future.done():
            future.result()
            raise AssertionError("operation finished without waiting for the lock holder")
        for pid, query in monitor.execute(
                "SELECT pid, query FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                (holder_pid,)).fetchall():
            if query_fragment in query:
                return pid
        time.sleep(0.02)
    raise AssertionError(f"no session blocked by {holder_pid} on {query_fragment!r}")


def result_of(future):
    try:
        return future.result(timeout=WAIT_SECONDS + 10)
    except Exception as exc:  # noqa: BLE001
        return exc


# =============================================================================
# Permission matrix through the real layer
# =============================================================================

@pytest.mark.db
@pytest.mark.parametrize("key", ["engineer_a", "manager_a"])
def test_engineer_and_manager_create_read_and_list_in_authorized_customer(world, key):
    cred, a = world.creds[key], world.org_a
    created = world.ops.create_case(cred, str(a), "Synthetic VPN outage", "Fictional detail")
    assert (created["organization_id"], created["status"], created["version"]) == (a, "NEW", 1)
    assert set(created) == {"id", "organization_id", "title", "description", "status",
                            "version", "created_at", "updated_at"}
    stored = world.admin.execute("SELECT title, description, status, version FROM case_record "
                                 "WHERE id = %s", (created["id"],)).fetchone()
    assert stored == ("Synthetic VPN outage", "Fictional detail", "NEW", 1)
    assert world.ops.get_case(cred, str(created["id"])) == created
    listed = world.ops.list_cases(cred, str(a))
    assert [item["id"] for item in listed["items"]] == [created["id"]]
    assert "description" not in listed["items"][0] and listed["has_more"] is False


@pytest.mark.db
def test_requester_creates_but_cannot_read_or_list(world):
    cred = world.creds["requester_a"]
    created = world.ops.create_case(cred, str(world.org_a), "Synthetic printer issue", "Fictional")
    assert created["status"] == "NEW"
    before = world.snapshot()
    assert isinstance(outcome(world.ops.get_case, cred, str(created["id"])), CaseNotFound)
    assert isinstance(outcome(world.ops.list_cases, cred, str(world.org_a)), OrganizationAccessDenied)
    assert isinstance(outcome(world.ops.create_case, cred, str(world.org_b), "x", "y"),
                      OrganizationAccessDenied)
    assert world.snapshot() == before


@pytest.mark.db
def test_engineer_of_a_cannot_create_read_or_list_b(world):
    b_case = world.seed_case(world.org_b, "Synthetic B-only case")
    cred, b = world.creds["engineer_a"], world.org_b
    before = world.snapshot()
    assert isinstance(outcome(world.ops.create_case, cred, str(b), "x", "y"), OrganizationAccessDenied)
    assert isinstance(outcome(world.ops.get_case, cred, str(b_case)), CaseNotFound)
    assert isinstance(outcome(world.ops.list_cases, cred, str(b)), OrganizationAccessDenied)
    assert world.snapshot() == before


@pytest.mark.db
def test_engineer_with_grants_in_a_and_b_accesses_each_separately(world):
    cred = world.creds["engineer_ab"]
    case_a = world.ops.create_case(cred, str(world.org_a), "Synthetic A case", "Fictional")
    case_b = world.ops.create_case(cred, str(world.org_b), "Synthetic B case", "Fictional")
    assert [i["id"] for i in world.ops.list_cases(cred, str(world.org_a))["items"]] == [case_a["id"]]
    assert [i["id"] for i in world.ops.list_cases(cred, str(world.org_b))["items"]] == [case_b["id"]]
    assert world.ops.get_case(cred, str(case_b["id"]))["organization_id"] == world.org_b
    world.prov.deactivate_grant(world.admin, world.actors["engineer_ab"], world.org_b)
    assert isinstance(outcome(world.ops.get_case, cred, str(case_b["id"])), CaseNotFound)
    assert isinstance(outcome(world.ops.list_cases, cred, str(world.org_b)), OrganizationAccessDenied)
    assert world.ops.get_case(cred, str(case_a["id"]))["id"] == case_a["id"]


@pytest.mark.db
def test_no_membership_and_platform_administrator_have_no_access(world):
    case_a = world.seed_case(world.org_a)
    before = world.snapshot()
    for key in ("engineer_b", "platform_admin"):
        cred = world.creds[key]
        assert isinstance(outcome(world.ops.create_case, cred, str(world.org_a), "x", "y"),
                          OrganizationAccessDenied)
        assert isinstance(outcome(world.ops.get_case, cred, str(case_a)), CaseNotFound)
        assert isinstance(outcome(world.ops.list_cases, cred, str(world.org_a)),
                          OrganizationAccessDenied)
    assert isinstance(outcome(world.ops.list_cases, world.creds["platform_admin"], str(world.org_b)),
                      OrganizationAccessDenied)
    assert world.snapshot() == before


@pytest.mark.db
def test_inactive_membership_denies(world):
    case_a = world.seed_case(world.org_a)
    world.prov.deactivate_grant(world.admin, world.actors["engineer_a"], world.org_a)
    cred = world.creds["engineer_a"]
    before = world.snapshot()
    assert isinstance(outcome(world.ops.create_case, cred, str(world.org_a), "x", "y"),
                      OrganizationAccessDenied)
    assert isinstance(outcome(world.ops.get_case, cred, str(case_a)), CaseNotFound)
    assert isinstance(outcome(world.ops.list_cases, cred, str(world.org_a)), OrganizationAccessDenied)
    assert world.snapshot() == before


@pytest.mark.db
def test_inactive_actor_denies_via_resolver_and_via_layer_check(world):
    case_a = world.seed_case(world.org_a)
    actor = world.actors["engineer_a"]
    world.admin.execute("UPDATE actor SET is_active = FALSE WHERE id = %s", (actor,))
    before = world.snapshot()
    cred = world.creds["engineer_a"]
    # Real resolver: refuses the inactive actor first.
    for call in (lambda: world.ops.create_case(cred, str(world.org_a), "x", "y"),
                 lambda: world.ops.get_case(cred, str(case_a)),
                 lambda: world.ops.list_cases(cred, str(world.org_a))):
        assert isinstance(outcome(call), auth.InactiveActor)
    # Stale identity (actor deactivated after resolution): the layer's own checks deny.
    stale = AuthorizedCaseOperations(StaticResolver({"stale": actor}), world.dsn)
    for call in (lambda: stale.create_case("stale", str(world.org_a), "x", "y"),
                 lambda: stale.get_case("stale", str(case_a)),
                 lambda: stale.list_cases("stale", str(world.org_a))):
        result = outcome(call)
        assert isinstance(result, auth.InactiveActor) and result.status_equivalent == 403
    assert world.snapshot() == before


@pytest.mark.db
def test_stale_identity_for_deleted_actor_is_unauthenticated(world):
    ghost = uuid4()
    ops = AuthorizedCaseOperations(StaticResolver({"ghost": ghost}), world.dsn)
    before = world.snapshot()
    for call in (lambda: ops.create_case("ghost", str(world.org_a), "x", "y"),
                 lambda: ops.get_case("ghost", str(uuid4())),
                 lambda: ops.list_cases("ghost", str(world.org_a))):
        result = outcome(call)
        assert isinstance(result, auth.Unauthenticated) and result.reason == "actor_not_found"
    assert world.snapshot() == before


@pytest.mark.db
def test_unknown_role_cannot_exist_and_unknown_action_has_no_entry_point(world):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        world.admin.execute("UPDATE organization_membership SET role = 'platform_administrator' "
                            "WHERE actor_id = %s", (world.actors["engineer_a"],))
    public = {n for n in dir(AuthorizedCaseOperations) if not n.startswith("_")}
    assert public == {"create_case", "get_case", "list_cases"}


@pytest.mark.db
def test_nonexistent_organization_denied_like_forbidden_one_without_writes(world):
    cred = world.creds["engineer_a"]
    before = world.snapshot()
    missing = outcome(world.ops.create_case, cred, str(uuid4()), "x", "y")
    forbidden = outcome(world.ops.create_case, cred, str(world.org_b), "x", "y")
    missing_list = outcome(world.ops.list_cases, cred, str(uuid4()))
    forbidden_list = outcome(world.ops.list_cases, cred, str(world.org_b))
    for a, b in ((missing, forbidden), (missing_list, forbidden_list)):
        assert type(a) is type(b) is OrganizationAccessDenied
        assert (str(a), a.args, a.reason, a.status_equivalent) == (str(b), b.args, b.reason, 403)
    assert world.snapshot() == before


@pytest.mark.db
def test_missing_foreign_and_unreadable_cases_are_indistinguishable(world):
    foreign = world.seed_case(world.org_b)
    own_by_requester = world.ops.create_case(world.creds["requester_a"], str(world.org_a), "x", "y")
    results = [
        outcome(world.ops.get_case, world.creds["engineer_a"], str(uuid4())),           # missing
        outcome(world.ops.get_case, world.creds["engineer_a"], str(foreign)),           # foreign
        outcome(world.ops.get_case, world.creds["requester_a"], str(own_by_requester["id"])),  # unreadable
        outcome(world.ops.get_case, world.creds["platform_admin"], str(foreign)),
    ]
    first = results[0]
    for r in results:
        assert type(r) is CaseNotFound
        assert (str(r), repr(r), r.args, r.reason, r.status_equivalent) == \
            (str(first), repr(first), first.args, first.reason, 404)


@pytest.mark.db
def test_spoofed_actor_and_tenant_identifiers_never_override_credential_identity(world):
    a_case = world.seed_case(world.org_a)
    before = world.snapshot()
    victim = world.actors["engineer_a"]
    # A constructed VerifiedActor, a raw actor UUID, or a display name is not a credential.
    for spoof in (auth.VerifiedActor(victim, "0123456789abcdef"), str(victim), victim,
                  "synthetic engineer_a"):
        assert isinstance(outcome(world.ops.get_case, spoof, str(a_case)), auth.Unauthenticated)
        assert isinstance(outcome(world.ops.list_cases, spoof, str(world.org_a)), auth.Unauthenticated)
        assert isinstance(outcome(world.ops.create_case, spoof, str(world.org_a), "x", "y"),
                          auth.Unauthenticated)
    # Extra identity or scope arguments are not accepted at all.
    with pytest.raises(TypeError):
        world.ops.get_case(world.creds["engineer_b"], str(a_case), actor_id=victim)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        world.ops.list_cases(world.creds["engineer_b"], str(world.org_a), authorized=True)  # type: ignore[call-arg]
    # B's engineer naming A as the tenant gets nothing.
    assert isinstance(outcome(world.ops.list_cases, world.creds["engineer_b"], str(world.org_a)),
                      OrganizationAccessDenied)
    assert world.snapshot() == before


@pytest.mark.db
def test_credential_revocation_denies_new_attempts_and_rotation_keeps_access(world):
    a_case = world.seed_case(world.org_a)
    actor = world.actors["engineer_a"]
    entry = next(e for e in auth.read_store(world.store) if e.actor_id == actor)
    new_cred, _ = auth.rotate_credential(world.store, entry.credential_id)
    old_cred = world.creds["engineer_a"]
    assert world.ops.get_case(new_cred, str(a_case))["id"] == a_case
    before = world.snapshot()
    for call in (lambda: world.ops.create_case(old_cred, str(world.org_a), "x", "y"),
                 lambda: world.ops.get_case(old_cred, str(a_case)),
                 lambda: world.ops.list_cases(old_cred, str(world.org_a))):
        result = outcome(call)
        assert isinstance(result, auth.Unauthenticated) and result.reason == "revoked"
    assert world.snapshot() == before


@pytest.mark.db
def test_database_outage_fails_closed_without_writes(world):
    a_case = world.seed_case(world.org_a)
    cred = world.creds["engineer_a"]
    before = world.snapshot()
    down = world.operations(UNREACHABLE_DSN, connect_timeout_s=2)
    for call in (lambda: down.create_case(cred, str(world.org_a), "x", "y"),
                 lambda: down.get_case(cred, str(a_case)),
                 lambda: down.list_cases(cred, str(world.org_a))):
        result = outcome(call)
        assert isinstance(result, CaseOperationUnavailable) and result.reason == "database"
        assert result.__cause__ is None and result.__context__ is None
    # The resolver's own database being down is an authentication dependency failure.
    resolver_down = AuthorizedCaseOperations(
        auth.CredentialResolver(auth.FileCredentialStore(world.store),
                                auth.PostgresActorLookup(UNREACHABLE_DSN, connect_timeout_s=2)),
        world.dsn)
    assert isinstance(outcome(resolver_down.list_cases, cred, str(world.org_a)),
                      auth.AuthenticationUnavailable)
    assert world.snapshot() == before


@pytest.mark.db
def test_list_is_bounded_ordered_and_single_tenant(world):
    cred = world.creds["engineer_ab"]
    made = [world.ops.create_case(cred, str(world.org_a), f"Synthetic case {i}", "Fictional")
            for i in range(5)]
    world.seed_case(world.org_b)
    expected = sorted(made, key=lambda c: (c["created_at"], str(c["id"])))
    page = world.ops.list_cases(cred, str(world.org_a), limit=2)
    assert [i["id"] for i in page["items"]] == [c["id"] for c in expected[:2]]
    assert page["has_more"] is True
    full = world.ops.list_cases(cred, str(world.org_a), limit=100)
    assert [i["id"] for i in full["items"]] == [c["id"] for c in expected]
    assert full["has_more"] is False
    assert all(i["organization_id"] == world.org_a for i in full["items"])
    assert world.ops.list_cases(world.creds["engineer_b"], str(world.org_b))["items"]  # B has one
    empty_org_engineer = world.creds["engineer_b"]
    world.admin.execute("DELETE FROM case_record WHERE organization_id = %s", (world.org_b,))
    assert world.ops.list_cases(empty_org_engineer, str(world.org_b)) == {"items": [], "has_more": False}


@pytest.mark.db
def test_unkeyed_create_is_not_idempotent(world):
    cred = world.creds["engineer_a"]
    first = world.ops.create_case(cred, str(world.org_a), "Same title", "Same description")
    second = world.ops.create_case(cred, str(world.org_a), "Same title", "Same description")
    assert first["id"] != second["id"]


@pytest.mark.db
def test_logs_contain_no_credentials_or_case_text(world, caplog):
    caplog.set_level(logging.DEBUG)
    secret_title, secret_desc = "Synthetic title " + uuid4().hex, "Synthetic description " + uuid4().hex
    created = world.ops.create_case(world.creds["engineer_a"], str(world.org_a), secret_title, secret_desc)
    world.ops.get_case(world.creds["engineer_a"], str(created["id"]))
    outcome(world.ops.get_case, world.creds["engineer_b"], str(created["id"]))
    outcome(world.ops.create_case, world.creds["engineer_b"], str(world.org_a), secret_title, secret_desc)
    text = caplog.text
    for value in [secret_title, secret_desc, *world.creds.values()]:
        assert value not in text
    assert "authz.allowed action=case:create" in text and "authz.denied action=case:read" in text


# =============================================================================
# Revocation concurrency for create (real sessions, controlled interleavings)
# =============================================================================

@pytest.mark.db
@pytest.mark.parametrize("revocation,stage,expected", [
    ("UPDATE organization_membership SET is_active = FALSE WHERE actor_id = %(actor)s AND organization_id = %(org)s",
     "FROM organization_membership", OrganizationAccessDenied),
    ("UPDATE actor SET is_active = FALSE WHERE id = %(actor)s",
     "FROM actor WHERE id", auth.InactiveActor),
])
def test_revocation_committing_first_makes_waiting_create_deny(world, pool, revocation, stage, expected):
    actor, org = world.actors["engineer_a"], world.org_a
    holder = world.conn()
    before = world.snapshot()
    with holder.transaction():
        holder.execute(revocation, {"actor": actor, "org": org})
        fut = pool.submit(world.ops.create_case, world.creds["engineer_a"], str(org), "x", "y")
        wait_blocked_by(world.admin, holder.info.backend_pid, stage, fut)
    assert isinstance(result_of(fut), expected)
    assert world.snapshot() == before  # no case written


@pytest.mark.db
def test_role_change_committing_first_is_seen_by_waiting_create(world, pool):
    """Every ADR-0006-eligible role keeps case:create, so this proves ordering, not denial."""
    actor, org = world.actors["engineer_a"], world.org_a
    holder = world.conn()
    with holder.transaction():
        holder.execute("UPDATE organization_membership SET role = 'service_manager' "
                       "WHERE actor_id = %s AND organization_id = %s", (actor, org))
        fut = pool.submit(world.ops.create_case, world.creds["engineer_a"], str(org), "x", "y")
        wait_blocked_by(world.admin, holder.info.backend_pid, "FROM organization_membership", fut)
    created = result_of(fut)
    assert isinstance(created, dict) and created["organization_id"] == org
    assert world.grant("engineer_a", org) == ("service_manager", True)


@pytest.mark.db
@pytest.mark.parametrize("revocation,blocked_fragment", [
    ("UPDATE organization_membership SET is_active = FALSE WHERE actor_id = %(actor)s AND organization_id = %(org)s",
     "UPDATE organization_membership"),
    ("UPDATE actor SET is_active = FALSE WHERE id = %(actor)s", "UPDATE actor"),
    ("UPDATE organization_membership SET role = 'service_manager' WHERE actor_id = %(actor)s AND organization_id = %(org)s",
     "UPDATE organization_membership"),
])
def test_create_holding_locks_makes_revocation_wait_until_commit(world, pool, revocation, blocked_fragment):
    actor, org = world.actors["engineer_a"], world.org_a
    gate, revoker = world.conn(), world.conn()
    count_before = world.snapshot()[1]
    with gate.transaction():
        # Hold the create at its INSERT, after it has taken both FOR SHARE locks.
        gate.execute("LOCK TABLE case_record IN SHARE MODE")
        create = pool.submit(world.ops.create_case, world.creds["engineer_a"], str(org),
                             "Synthetic in-flight case", "Fictional")
        create_pid = wait_blocked_by(world.admin, gate.info.backend_pid, "INSERT INTO case_record", create)
        revoke = pool.submit(revoker.execute, revocation, {"actor": actor, "org": org})
        # The revocation waits for the create's row locks, not for the gate.
        wait_blocked_by(world.admin, create_pid, blocked_fragment, revoke)
        assert not revoke.done()
    created = result_of(create)
    assert isinstance(created, dict), created          # the in-flight create completed
    assert result_of(revoke) is not None and not isinstance(result_of(revoke), Exception)
    row = world.admin.execute("SELECT organization_id, status FROM case_record WHERE id = %s",
                              (created["id"],)).fetchone()
    assert row == (org, "NEW") and world.snapshot()[1] == count_before + 1
    if "is_active = FALSE" in revocation:              # and the next attempt is denied
        nxt = outcome(world.ops.create_case, world.creds["engineer_a"], str(org), "x", "y")
        assert isinstance(nxt, (OrganizationAccessDenied, auth.InactiveActor))
        assert world.snapshot()[1] == count_before + 1


@pytest.mark.db
@pytest.mark.parametrize("held", [
    "UPDATE organization_membership SET is_active = FALSE WHERE actor_id = %(actor)s AND organization_id = %(org)s",
    "UPDATE actor SET is_active = FALSE WHERE id = %(actor)s",
])
def test_lock_timeout_is_unavailable_not_denial_and_writes_nothing(world, held):
    actor, org = world.actors["engineer_a"], world.org_a
    holder = world.conn()
    impatient = world.operations(lock_timeout_ms=300)
    before = world.snapshot()
    with holder.transaction():
        holder.execute(held, {"actor": actor, "org": org})
        started = time.monotonic()
        result = outcome(impatient.create_case, world.creds["engineer_a"], str(org), "x", "y")
        elapsed = time.monotonic() - started
    assert isinstance(result, CaseOperationUnavailable) and result.reason == "lock_timeout"
    assert 0.25 <= elapsed < 8
    assert world.snapshot() == before


@pytest.mark.db
def test_backend_failure_during_write_is_unavailable_and_writes_nothing(world, pool):
    org = world.org_a
    gate = world.conn()
    before = world.snapshot()
    with gate.transaction():
        gate.execute("LOCK TABLE case_record IN SHARE MODE")
        create = pool.submit(world.ops.create_case, world.creds["engineer_a"], str(org), "x", "y")
        create_pid = wait_blocked_by(world.admin, gate.info.backend_pid, "INSERT INTO case_record", create)
        world.admin.execute("SELECT pg_terminate_backend(%s)", (create_pid,))
        result = result_of(create)
    assert isinstance(result, CaseOperationUnavailable)
    assert world.snapshot() == before


@pytest.mark.db
def test_commit_failure_is_reported_as_unknown_outcome_never_success(world, monkeypatch):
    import psycopg

    real_connect = AuthorizedCaseOperations._connect

    def connect_with_failing_commit(self, *, autocommit):
        conn = real_connect(self, autocommit=autocommit)
        if not autocommit:  # only the create transaction's connection
            def failing_commit():
                raise psycopg.OperationalError("simulated connection loss during COMMIT")
            conn.commit = failing_commit
        return conn

    monkeypatch.setattr(AuthorizedCaseOperations, "_connect", connect_with_failing_commit)
    result = outcome(world.ops.create_case, world.creds["engineer_a"], str(world.org_a), "x", "y")
    monkeypatch.undo()
    assert isinstance(result, CaseCreationOutcomeUnknown) and result.status_equivalent == 500
    assert result.__context__ is None
    # In this simulation COMMIT was never sent, so the server rolled back; the
    # layer cannot know that and correctly reports the outcome as unknown.
    assert world.snapshot()[0] == []


@pytest.mark.db
@pytest.mark.parametrize("phase,expected", [
    ("write", CaseOperationUnavailable),
    ("commit", CaseCreationOutcomeUnknown),
])
def test_unexpected_non_driver_error_fails_closed_with_correct_outcome(world, monkeypatch, phase, expected):
    """Readiness-review regression: errors that are not psycopg errors are still mapped.

    During the write they mean the operation did not complete (unavailable);
    during COMMIT the outcome is unknown. Neither may escape raw, be reported as
    success, or chain the original exception (which could carry case text).
    """
    real_connect = AuthorizedCaseOperations._connect
    secret_text = "Synthetic private description 7f3a"

    class Boom(RuntimeError):
        pass

    def connect_with_fault(self, *, autocommit):
        conn = real_connect(self, autocommit=autocommit)
        if not autocommit:
            if phase == "commit":
                def failing_commit():
                    raise Boom(secret_text)
                conn.commit = failing_commit
            else:
                real_execute = conn.execute

                def failing_execute(query, params=None, **kw):
                    if "INSERT INTO case_record" in str(query):
                        raise Boom(secret_text)
                    return real_execute(query, params, **kw)
                conn.execute = failing_execute
        return conn

    before = world.snapshot()
    monkeypatch.setattr(AuthorizedCaseOperations, "_connect", connect_with_fault)
    result = outcome(world.ops.create_case, world.creds["engineer_a"], str(world.org_a),
                     "Synthetic title", secret_text)
    monkeypatch.undo()
    assert type(result) is expected, result
    assert result.__cause__ is None and result.__context__ is None
    assert secret_text not in repr(result) and secret_text not in str(result)
    if phase == "write":
        assert result.reason == "internal"
    # COMMIT was never sent in either simulation, so nothing was persisted.
    assert world.snapshot() == before


# =============================================================================
# Read-side snapshot behavior (no claim of instantaneous revocation)
# =============================================================================

@pytest.mark.db
def test_reads_do_not_wait_for_uncommitted_revocation_and_deny_after_commit(world):
    a_case = world.seed_case(world.org_a)
    actor, org, cred = world.actors["engineer_a"], world.org_a, world.creds["engineer_a"]
    holder = world.conn()
    with holder.transaction():
        holder.execute("UPDATE organization_membership SET is_active = FALSE "
                       "WHERE actor_id = %s AND organization_id = %s", (actor, org))
        started = time.monotonic()
        # Uncommitted revocation: reads take no row locks and see the committed state.
        assert world.ops.get_case(cred, str(a_case))["id"] == a_case
        assert [i["id"] for i in world.ops.list_cases(cred, str(org))["items"]] == [a_case]
        assert time.monotonic() - started < 5  # did not wait for the uncommitted revocation
    # After the revocation commits, new reads are denied.
    assert isinstance(outcome(world.ops.get_case, cred, str(a_case)), CaseNotFound)
    assert isinstance(outcome(world.ops.list_cases, cred, str(org)), OrganizationAccessDenied)


def test_permission_matrix_is_read_only_at_runtime_offline():
    """Readiness-review hardening: in-process code cannot widen the matrix."""
    with pytest.raises(TypeError):
        policy.ROLE_PERMISSIONS["requester"] = frozenset(policy.ACTIONS)  # type: ignore[index]
    assert policy.permits("requester", "case:read") is False


def test_factory_builds_the_layer_with_the_real_resolver_offline(tmp_path):
    """Routes may import only this factory, so they cannot inject their own resolver."""
    from src.authorized_case_operations import operations_from_environment

    store = tmp_path / "credentials.json"
    auth.write_store(store, [])
    ops = operations_from_environment(UNREACHABLE_DSN, store)
    assert isinstance(ops, AuthorizedCaseOperations)
    assert type(ops._resolver) is auth.CredentialResolver
    with pytest.raises(auth.Unauthenticated):
        ops.get_case(auth.generate_credential(), str(uuid4()))  # unknown: no DB needed
