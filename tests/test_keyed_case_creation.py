"""Keyed idempotent case creation (ADR-0004) against PostgreSQL, mostly over HTTP.

Every test runs on the dedicated ``eaiso_test`` database (the guards of the
centralized-authorization suite, which also require ``db/004``). Requests go
through the real FastAPI application, the trusted factory, the credential
resolver, and ``AuthorizedCaseOperations``. Concurrency tests coordinate real
PostgreSQL sessions and treat a session as waiting only when
``pg_blocking_pids()`` names the holder. Fault-injection tests say whether the
fault is real (terminated backend, cut TCP connection) or simulated (a
patched driver call). Each test asserts the persisted case and idempotency
records, not only HTTP codes.
"""

# Fixtures are imported from the centralized-authorization suite and used as
# test parameters, which ruff reports as unused imports and redefinitions.
# ruff: noqa: F401, F811

from __future__ import annotations

import logging
import time
import unicodedata
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_authorization_readiness import CommitCuttingProxy
from test_case_authorization import (
    WAIT_SECONDS,
    authz_dsn,
    pool,
    result_of,
    wait_blocked_by,
    world,
)

from src import credential_auth as auth
from src.api.app import create_app
from src.authorized_case_operations import AuthorizedCaseOperations
from src.request_fingerprint import case_create_fingerprint

pytestmark = pytest.mark.db

MISMATCH = {"error": {"code": "idempotency_payload_mismatch",
                      "message": "Idempotency key was already used with a different request"}}
IN_FLIGHT = {"error": {"code": "idempotency_request_in_flight",
                       "message": "A request with this idempotency key is still in progress. "
                                  "Retry with the same key."}}
ORG_DENIED = {"error": {"code": "organization_access_denied",
                        "message": "Not permitted for this organization"}}
INACTIVE = {"error": {"code": "actor_inactive", "message": "Actor is not active"}}
UNAUTHENTICATED = {"error": {"code": "unauthenticated", "message": "Authentication required"}}
KEYED_UNKNOWN = {"error": {"code": "case_creation_outcome_unknown",
                           "message": "The case may or may not have been created. Retry with the same "
                                      "Idempotency-Key and payload to learn the outcome."}}
INTERNAL = {"error": {"code": "internal_error", "message": "Internal server error"}}
TITLE, DESCRIPTION = "Synthetic keyed VPN outage", "Fictional keyed detail"


def new_key() -> str:
    return f"{uuid4()}"


def body_of(response):
    data = response.json()
    if "error" in data:
        data = {"error": {k: v for k, v in data["error"].items() if k != "request_id"}}
    return data


def assert_error(response, status, expected):
    assert response.status_code == status, (response.status_code, response.text)
    assert body_of(response) == expected


def make_client(world, monkeypatch, *, dsn=None, wait_ms=None):
    monkeypatch.setenv("DATABASE_URL", dsn or world.dsn)
    monkeypatch.setenv("EAISO_CREDENTIALS_FILE", str(world.store))
    if wait_ms is None:
        monkeypatch.delenv("EAISO_IDEMPOTENCY_WAIT_MS", raising=False)
    else:
        monkeypatch.setenv("EAISO_IDEMPOTENCY_WAIT_MS", str(wait_ms))
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def api(world, monkeypatch):
    with make_client(world, monkeypatch) as client:
        yield client


def post(client, world, actor, org, key, title=TITLE, description=DESCRIPTION, *, credential=None,
         raw=None, headers=None):
    all_headers = {"Authorization": f"Bearer {credential or world.creds[actor]}",
                   "Content-Type": "application/json", **(headers or {})}
    if key is not None:
        all_headers["Idempotency-Key"] = f'"{key}"'
    if raw is not None:
        return client.post("/cases", headers=all_headers, content=raw)
    return client.post("/cases", headers=all_headers,
                       json={"organization_id": str(org), "title": title, "description": description})


def records(world):
    return world.admin.execute(
        "SELECT organization_id, actor_id, operation, idempotency_key, fingerprint_version, "
        "request_fingerprint, case_id FROM case_creation_idempotency "
        "WHERE organization_id = ANY(%s) ORDER BY created_at, case_id", (world.orgs,)).fetchall()


def state(world):
    """Cases and idempotency records of the test customers."""
    return world.snapshot()[0], records(world)


def assert_no_partial_pairs(world):
    orphan_records = world.admin.execute(
        "SELECT count(*) FROM case_creation_idempotency i LEFT JOIN case_record c "
        "ON c.id = i.case_id AND c.organization_id = i.organization_id "
        "WHERE i.organization_id = ANY(%s) AND c.id IS NULL", (world.orgs,)).fetchone()[0]
    assert orphan_records == 0


def record_queries(monkeypatch, ops):
    """Record every SQL statement the layer sends, without changing behavior."""
    seen: list[str] = []
    real_connect = ops._connect

    def connect(*, autocommit):
        conn = real_connect(autocommit=autocommit)
        real_execute = conn.execute

        def execute(query, params=None, **kw):
            seen.append(str(query))
            return real_execute(query, params, **kw)

        conn.execute = execute
        return conn

    monkeypatch.setattr(ops, "_connect", connect)
    return seen


# =============================================================================
# Migration and integrity constraints
# =============================================================================

def test_migration_constraints_hold(world):
    import psycopg
    from psycopg import errors

    a_case, b_case = world.seed_case(world.org_a), world.seed_case(world.org_b)
    actor, fp = world.actors["engineer_a"], "a" * 64
    insert = ("INSERT INTO case_creation_idempotency (organization_id, actor_id, operation, idempotency_key, "
              "fingerprint_version, request_fingerprint, case_id) VALUES (%s, %s, %s, %s, %s, %s, %s)")

    def attempt(*values):
        try:
            with world.admin.transaction():
                world.admin.execute(insert, values)
        except psycopg.Error as exc:
            return type(exc)
        return None

    key = new_key()
    assert attempt(world.org_a, actor, "case:create", key, 1, fp, a_case) is None
    assert attempt(world.org_a, actor, "case:create", key, 1, fp, world.seed_case(world.org_a)) \
        is errors.UniqueViolation                                          # duplicate scope
    assert attempt(world.org_a, actor, "case:create", new_key(), 1, fp, a_case) \
        is errors.UniqueViolation                                          # one record per case
    assert attempt(world.org_a, actor, "case:create", new_key(), 1, fp, b_case) \
        is errors.ForeignKeyViolation                                      # case of another customer
    assert attempt(world.org_a, actor, "case:create", new_key(), 1, fp, uuid4()) \
        is errors.ForeignKeyViolation                                      # nonexistent case
    assert attempt(world.org_a, actor, "case:create", new_key(), 1, fp, None) is errors.NotNullViolation
    fresh = world.seed_case(world.org_a)
    for bad in (("case:read", new_key(), 1, fp), ("case:create", "too-short", 1, fp),
                ("case:create", "x" * 129, 1, fp), ("case:create", "has space 0000000000", 1, fp),
                ("case:create", new_key(), 2, fp), ("case:create", new_key(), 1, "A" * 64),
                ("case:create", new_key(), 1, "a" * 63)):
        assert attempt(world.org_a, actor, *bad, fresh) is errors.CheckViolation, bad
    assert attempt(world.org_a, uuid4(), "case:create", new_key(), 1, fp, fresh) is errors.ForeignKeyViolation
    # The ownership foreign key is checked at COMMIT, not at INSERT: a record may be
    # inserted before its case in the same transaction (reserve-first), and a record
    # whose case never appears fails only when the transaction commits.
    reserve_first = uuid4()
    with world.admin.transaction():
        world.admin.execute(insert, (world.org_a, actor, "case:create", new_key(), 1, fp, reserve_first))
        world.admin.execute("INSERT INTO case_record (id, organization_id, title, description) "
                            "VALUES (%s, %s, 'Synthetic reserve-first', 'Fictional')", (reserve_first, world.org_a))
    orphan = psycopg.connect(world.dsn)
    try:
        orphan.execute(insert, (world.org_a, actor, "case:create", new_key(), 1, fp, uuid4()))  # accepted for now
        with pytest.raises(errors.ForeignKeyViolation):
            orphan.commit()
    finally:
        orphan.close()
    with pytest.raises(errors.ForeignKeyViolation):                        # a keyed case cannot be deleted
        world.admin.execute("DELETE FROM case_record WHERE id = %s", (a_case,))
    assert world.admin.execute("SELECT count(*) FROM case_creation_idempotency WHERE organization_id = ANY(%s)",
                               (world.orgs,)).fetchone()[0] == 2


# =============================================================================
# Sequential key behavior (HTTP)
# =============================================================================

def test_first_keyed_create_then_replay_then_mismatch(api, world):
    key, org, actor = new_key(), world.org_a, world.actors["engineer_a"]
    first = post(api, world, "engineer_a", org, key)
    assert first.status_code == 201
    payload = first.json()
    assert first.headers["location"] == f"/cases/{payload['id']}"
    assert "idempotency-replayed" not in first.headers
    assert (payload["status"], payload["version"], payload["title"]) == ("NEW", 1, TITLE)
    cases, recs = state(world)
    assert [str(c[0]) for c in cases] == [payload["id"]]
    assert recs == [(org, actor, "case:create", key, 1, case_create_fingerprint(org, TITLE, DESCRIPTION),
                     UUID(payload["id"]))]

    replay = post(api, world, "engineer_a", org, key)
    assert replay.status_code == 200
    assert replay.json() == {"id": payload["id"], "organization_id": str(org)}
    assert replay.headers["location"] == f"/cases/{payload['id']}"
    assert replay.headers["idempotency-replayed"] == "true"
    assert state(world) == (cases, recs)

    changed = post(api, world, "engineer_a", org, key, description=DESCRIPTION + " (edited)")
    assert_error(changed, 422, MISMATCH)
    assert TITLE not in changed.text and DESCRIPTION not in changed.text
    assert state(world) == (cases, recs)
    assert_no_partial_pairs(world)


@pytest.mark.parametrize("raw,outcome", [
    ('{"title": "%s", "description": "%s", "organization_id": "%s"}', 200),        # reordered
    ('{\n  "organization_id" : "%s" ,\n\t"title":"%s","description":   "%s"  }', 200),  # whitespace
])
def test_reordered_and_whitespace_json_replays(api, world, raw, outcome):
    key, org = new_key(), world.org_a
    assert post(api, world, "engineer_a", org, key).status_code == 201
    if raw.startswith('{"title"'):
        body = raw % (TITLE, DESCRIPTION, org)
    else:
        body = raw % (org, TITLE, DESCRIPTION)
    assert post(api, world, "engineer_a", org, key, raw=body.encode()).status_code == outcome
    assert len(records(world)) == 1


def test_fingerprint_equivalence_and_distinctness_over_http(api, world):
    org, key = world.org_a, new_key()
    nfc = "Café outage"
    assert post(api, world, "engineer_a", org, key, title=nfc).status_code == 201
    escaped = ('{"organization_id": "' + str(org) + '", "title": "Caf\\u00e9 outage", '
               '"description": "' + DESCRIPTION + '"}').encode()
    assert post(api, world, "engineer_a", org, key, raw=escaped).status_code == 200
    upper_org = post(api, world, "engineer_a", str(org).upper(), key, title=nfc)
    assert upper_org.status_code == 200 and upper_org.json()["organization_id"] == str(org)
    for variant in (unicodedata.normalize("NFD", nfc), nfc + " ", nfc.upper()):
        assert_error(post(api, world, "engineer_a", org, key, title=variant), 422, MISMATCH)
    assert len(state(world)[0]) == 1 and len(records(world)) == 1


def test_distinct_keys_and_unkeyed_requests_create_distinct_cases(api, world):
    org = world.org_a
    keyed = [post(api, world, "engineer_a", org, new_key()) for _ in range(2)]
    unkeyed = [post(api, world, "engineer_a", org, None) for _ in range(2)]
    assert [r.status_code for r in keyed + unkeyed] == [201] * 4
    assert len({r.json()["id"] for r in keyed + unkeyed}) == 4
    assert len(state(world)[0]) == 4 and len(records(world)) == 2   # unkeyed creates write no record


def test_malformed_keys_never_mutate(api, world):
    before = state(world)
    for value in ("unquoted-key-000000000", '"short"', "", '"a-key-0000000000000", "b-key-0000000000000"',
                  '"' + "k" * 129 + '"', "1234567890", "?1", "token-item-0000000000", '"caf\u00e9-key-0000000"'.encode()):
        response = api.post("/cases", headers=[("Authorization", f"Bearer {world.creds['engineer_a']}"),
                                               ("Content-Type", "application/json"), ("Idempotency-Key", value)],
                            json={"organization_id": str(world.org_a), "title": TITLE, "description": DESCRIPTION})
        assert response.status_code == 400, value
        assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert state(world) == before


def test_keys_differing_only_in_letter_case_are_distinct(api, world):
    key = "Case-Sensitive-Key-" + uuid4().hex[:8]
    lower = post(api, world, "engineer_a", world.org_a, key.lower())
    mixed = post(api, world, "engineer_a", world.org_a, key)
    assert (lower.status_code, mixed.status_code) == (201, 201)
    assert lower.json()["id"] != mixed.json()["id"]
    assert sorted(r[3] for r in records(world)) == sorted([key.lower(), key])


def test_duplicate_json_properties_with_a_key_are_rejected_without_mutation(api, world):
    key, org = new_key(), world.org_a
    raw = ('{"organization_id": "' + str(org) + '", "title": "First", "title": "Second", '
           '"description": "' + DESCRIPTION + '"}').encode()
    response = post(api, world, "engineer_a", org, key, raw=raw)
    assert response.status_code == 400 and response.json()["error"]["code"] == "malformed_json"
    assert state(world) == ([], [])
    # The key was never reserved: a valid request with it creates the case.
    assert post(api, world, "engineer_a", org, key).status_code == 201


# =============================================================================
# Actor and customer isolation (HTTP)
# =============================================================================

def test_two_actors_with_the_same_key_are_independent(api, world):
    key, org = new_key(), world.org_a
    first = post(api, world, "engineer_a", org, key)
    same_payload = post(api, world, "manager_a", org, key)
    changed_payload = post(api, world, "requester_a", org, key, description="Different text")
    assert [r.status_code for r in (first, same_payload, changed_payload)] == [201, 201, 201]
    assert len({r.json()["id"] for r in (first, same_payload, changed_payload)}) == 3
    assert sorted(r[1] for r in records(world)) == sorted(
        world.actors[k] for k in ("engineer_a", "manager_a", "requester_a"))


def test_same_actor_and_key_in_two_customers_are_independent(api, world):
    key = new_key()
    in_a = post(api, world, "engineer_ab", world.org_a, key)
    in_b = post(api, world, "engineer_ab", world.org_b, key)
    assert (in_a.status_code, in_b.status_code) == (201, 201)
    assert in_a.json()["organization_id"] == str(world.org_a)
    assert in_b.json()["organization_id"] == str(world.org_b)
    assert {(r[0], r[6]) for r in records(world)} == {(world.org_a, UUID(in_a.json()["id"])),
                                                      (world.org_b, UUID(in_b.json()["id"]))}


def test_credential_rotation_preserves_replay(api, world):
    key, org = new_key(), world.org_a
    created = post(api, world, "engineer_a", org, key).json()
    entry = next(e for e in auth.read_store(world.store) if e.actor_id == world.actors["engineer_a"])
    new_cred, _ = auth.rotate_credential(world.store, entry.credential_id)
    assert_error(post(api, world, "engineer_a", org, key), 401, UNAUTHENTICATED)  # old credential
    replay = post(api, world, "engineer_a", org, key, credential=new_cred)
    assert replay.status_code == 200 and replay.json()["id"] == created["id"]


@pytest.mark.parametrize("revocation", ["membership", "actor", "credential"])
def test_revocation_denies_replay_before_any_key_lookup(api, world, monkeypatch, revocation):
    key, org, actor = new_key(), world.org_a, world.actors["engineer_a"]
    assert post(api, world, "engineer_a", org, key).status_code == 201
    if revocation == "membership":
        world.prov.deactivate_grant(world.admin, actor, org)
        expected = (403, ORG_DENIED)
    elif revocation == "actor":
        world.admin.execute("UPDATE actor SET is_active = FALSE WHERE id = %s", (actor,))
        expected = (403, INACTIVE)
    else:
        entry = next(e for e in auth.read_store(world.store) if e.actor_id == actor)
        auth.revoke_credential(world.store, entry.credential_id)
        expected = (401, UNAUTHENTICATED)
    queries = record_queries(monkeypatch, api.app.state.case_operations)
    before = state(world)
    response = post(api, world, "engineer_a", org, key)
    assert_error(response, *expected)
    assert "id" not in response.json() and "location" not in response.headers
    assert not any("case_creation_idempotency" in q for q in queries)   # authorization came first
    assert state(world) == before


def test_wrong_or_nonexistent_organization_selector_never_reaches_the_key(api, world, monkeypatch):
    key = new_key()
    assert post(api, world, "engineer_a", world.org_a, key).status_code == 201
    queries = record_queries(monkeypatch, api.app.state.case_operations)
    before = state(world)
    for org in (world.org_b, uuid4()):
        assert_error(post(api, world, "engineer_a", org, key), 403, ORG_DENIED)
    assert not any("case_creation_idempotency" in q for q in queries)
    assert state(world) == before


def test_requester_replay_is_identity_only_even_after_the_case_changed(api, world, monkeypatch):
    key, org = new_key(), world.org_a
    created = post(api, world, "requester_a", org, key)
    assert created.status_code == 201
    case_id = created.json()["id"]
    world.admin.execute("UPDATE case_record SET title = 'Changed by staff', status = 'IN_REVIEW', "
                        "version = version + 1 WHERE id = %s", (case_id,))
    queries = record_queries(monkeypatch, api.app.state.case_operations)
    replay = post(api, world, "requester_a", org, key)
    assert replay.status_code == 200
    assert replay.json() == {"id": case_id, "organization_id": str(org)}
    for disclosed in ("Changed by staff", "IN_REVIEW", "version", "title", "status", "description"):
        assert disclosed not in replay.text
    assert not any("case_record" in q for q in queries)                 # the case is never read
    assert api.get(f"/cases/{case_id}", headers={"Authorization": f"Bearer {world.creds['requester_a']}"}
                   ).status_code == 404                                 # replay is not case:read


# =============================================================================
# Concurrency (real sessions, controlled interleavings)
# =============================================================================

def hold_create_at_insert(world, pool, client, actor, org, key, **kw):
    """Start a keyed create and hold it at the case INSERT, after its reservation."""
    gate = world.conn()
    gate.execute("BEGIN")
    gate.execute("LOCK TABLE case_record IN SHARE MODE")
    future = pool.submit(post, client, world, actor, org, key, **kw)
    pid = wait_blocked_by(world.admin, gate.info.backend_pid, "INSERT INTO case_record", future)
    return gate, future, pid


@pytest.mark.parametrize("second_description,second_status", [(DESCRIPTION, 200), ("Different", 422)])
def test_concurrent_same_key_requests_produce_one_case(api, world, pool, second_description, second_status):
    key, org = new_key(), world.org_a
    gate, first, first_pid = hold_create_at_insert(world, pool, api, "engineer_a", org, key)
    second = None
    try:
        second = pool.submit(post, api, world, "engineer_a", org, key, description=second_description)
        # The second request waits on the first one's uncommitted reservation.
        wait_blocked_by(world.admin, first_pid, "INSERT INTO case_creation_idempotency", second)
    finally:
        # Resolve every in-flight request (bounded) before the client can shut down.
        gate.execute("COMMIT")
        first_response, second_response = result_of(first), second and result_of(second)
    assert first_response.status_code == 201
    assert second_response.status_code == second_status
    if second_status == 200:
        assert second_response.json() == {"id": first_response.json()["id"], "organization_id": str(org)}
    else:
        assert_error(second_response, 422, MISMATCH)
    assert len(state(world)[0]) == 1 and len(records(world)) == 1
    assert_no_partial_pairs(world)


def test_winner_rolling_back_lets_the_waiting_request_create_once(api, world, pool):
    key, org = new_key(), world.org_a
    gate, first, first_pid = hold_create_at_insert(world, pool, api, "engineer_a", org, key)
    second = None
    try:
        second = pool.submit(post, api, world, "engineer_a", org, key)
        wait_blocked_by(world.admin, first_pid, "INSERT INTO case_creation_idempotency", second)
        world.admin.execute("SELECT pg_terminate_backend(%s)", (first_pid,))   # real fault: winner rolls back
        # The second request now wins the reservation and waits at the case INSERT.
        wait_blocked_by(world.admin, gate.info.backend_pid, "INSERT INTO case_record", second)
    finally:
        gate.execute("COMMIT")
        loser, winner = result_of(first), second and result_of(second)
    assert_error(loser, 500, INTERNAL)
    assert winner.status_code == 201
    cases, recs = state(world)
    assert [str(c[0]) for c in cases] == [winner.json()["id"]] and len(recs) == 1
    assert_no_partial_pairs(world)


def test_bounded_wait_returns_503_then_retry_resolves(world, pool, monkeypatch):
    key, org = new_key(), world.org_a
    with make_client(world, monkeypatch) as patient, make_client(world, monkeypatch, wait_ms=300) as impatient:
        gate, first, _ = hold_create_at_insert(world, pool, patient, "engineer_a", org, key)
        try:
            started = time.monotonic()
            waited = post(impatient, world, "engineer_a", org, key)
            elapsed = time.monotonic() - started
            cases_while_held = len(state(world)[0])
        finally:
            # Resolve the held request (bounded) before either client can shut down.
            gate.execute("COMMIT")
            created = result_of(first)
        retry = post(impatient, world, "engineer_a", org, key)
    assert_error(waited, 503, IN_FLIGHT)
    assert waited.json()["error"]["request_id"] == waited.headers["x-request-id"]
    assert 0.25 <= elapsed < 5
    assert cases_while_held == 0                                   # nothing committed yet, nothing extra
    assert created.status_code == 201
    assert retry.status_code == 200 and retry.json()["id"] == created.json()["id"]
    assert len(state(world)[0]) == 1 and len(records(world)) == 1


@pytest.mark.parametrize("holder_outcome", ["commit", "rollback"])
def test_unrelated_key_proceeds_while_a_reservation_is_held(api, world, pool, holder_outcome):
    """A manual session holds an uncommitted case and reservation for key K."""
    org, actor, key, other_key = world.org_a, world.actors["engineer_a"], new_key(), new_key()
    holder = world.conn()
    held_case = uuid4()
    holder.execute("BEGIN")
    holder.execute("INSERT INTO case_record (id, organization_id, title, description) VALUES (%s, %s, %s, %s)",
                   (held_case, org, TITLE, DESCRIPTION))
    holder.execute("INSERT INTO case_creation_idempotency (organization_id, actor_id, operation, idempotency_key, "
                   "fingerprint_version, request_fingerprint, case_id) VALUES (%s, %s, 'case:create', %s, 1, %s, %s)",
                   (org, actor, key, case_create_fingerprint(org, TITLE, DESCRIPTION), held_case))
    same = None
    try:
        started = time.monotonic()
        independent = post(api, world, "engineer_a", org, other_key)
        independent_elapsed = time.monotonic() - started
        same = pool.submit(post, api, world, "engineer_a", org, key)
        wait_blocked_by(world.admin, holder.info.backend_pid, "INSERT INTO case_creation_idempotency", same)
    finally:
        holder.execute(holder_outcome.upper())
        response = same and result_of(same)
    assert independent.status_code == 201 and independent_elapsed < 1.5
    if holder_outcome == "commit":
        assert response.status_code == 200 and response.json()["id"] == str(held_case)
    else:
        assert response.status_code == 201 and response.json()["id"] != str(held_case)
    assert len(state(world)[0]) == 2 and len(records(world)) == 2
    assert_no_partial_pairs(world)


def test_revocation_committing_first_denies_a_waiting_keyed_replay(api, world, pool, monkeypatch):
    key, org, actor = new_key(), world.org_a, world.actors["engineer_a"]
    assert post(api, world, "engineer_a", org, key).status_code == 201
    queries = record_queries(monkeypatch, api.app.state.case_operations)
    before = state(world)
    holder = world.conn()
    replay = None
    try:
        with holder.transaction():
            holder.execute("UPDATE organization_membership SET is_active = FALSE "
                           "WHERE actor_id = %s AND organization_id = %s", (actor, org))
            replay = pool.submit(post, api, world, "engineer_a", org, key)
            wait_blocked_by(world.admin, holder.info.backend_pid, "FROM organization_membership", replay)
    finally:
        replayed = replay and result_of(replay)
    assert_error(replayed, 403, ORG_DENIED)
    assert not any("case_creation_idempotency" in q for q in queries)
    assert state(world) == before


# =============================================================================
# Failure and recovery
# =============================================================================

def test_simulated_failure_during_reservation_leaves_nothing_and_retry_creates(api, world, monkeypatch):
    """Simulated: the driver call for the reservation INSERT raises."""
    import psycopg

    ops = api.app.state.case_operations
    real_connect = ops._connect

    def connect(*, autocommit):
        conn = real_connect(autocommit=autocommit)
        real_execute = conn.execute

        def execute(query, params=None, **kw):
            if "INSERT INTO case_creation_idempotency" in str(query):
                raise psycopg.OperationalError("simulated failure during reservation")
            return real_execute(query, params, **kw)

        conn.execute = execute
        return conn

    key, org = new_key(), world.org_a
    monkeypatch.setattr(ops, "_connect", connect)
    assert_error(post(api, world, "engineer_a", org, key), 500, INTERNAL)
    monkeypatch.undo()
    assert state(world) == ([], [])
    assert post(api, world, "engineer_a", org, key).status_code == 201
    assert len(state(world)[0]) == 1


def test_simulated_commit_failure_is_unknown_and_retry_resolves(api, world, monkeypatch):
    """Simulated: COMMIT raises before being sent, so the server rolls back."""
    import psycopg

    ops = api.app.state.case_operations
    real_connect = ops._connect

    def connect(*, autocommit):
        conn = real_connect(autocommit=autocommit)
        if not autocommit:
            def failing_commit():
                raise psycopg.OperationalError("simulated connection loss during COMMIT")
            conn.commit = failing_commit
        return conn

    key, org = new_key(), world.org_a
    monkeypatch.setattr(ops, "_connect", connect)
    unknown = post(api, world, "engineer_a", org, key)
    monkeypatch.undo()
    assert_error(unknown, 500, KEYED_UNKNOWN)
    assert "location" not in unknown.headers
    assert state(world) == ([], [])                       # case and record were inserted, then discarded
    assert post(api, world, "engineer_a", org, key).status_code == 201   # rolled-back outcome: create once
    assert len(state(world)[0]) == 1 and len(records(world)) == 1


def test_real_backend_failure_after_reservation_leaves_no_partial_pair(api, world, pool):
    """Real fault: the backend is terminated after the reservation, at the case INSERT."""
    key, org = new_key(), world.org_a
    gate, first, first_pid = hold_create_at_insert(world, pool, api, "engineer_a", org, key)
    try:
        world.admin.execute("SELECT pg_terminate_backend(%s)", (first_pid,))
    finally:
        gate.execute("COMMIT")
        response = result_of(first)
    assert_error(response, 500, INTERNAL)
    assert state(world) == ([], [])
    assert post(api, world, "engineer_a", org, key).status_code == 201
    assert_no_partial_pairs(world)


@pytest.mark.parametrize("mode,persisted", [("before_server", False), ("after_server", True)])
def test_real_lost_commit_response_is_resolved_by_retrying_with_the_same_key(world, monkeypatch, mode, persisted):
    """Real fault: a TCP proxy cuts the connection at COMMIT (before or after it reaches PostgreSQL)."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(world.dsn)
    proxy = CommitCuttingProxy(params.get("host") or "127.0.0.1", int(params.get("port") or 5432), mode)
    key, org = new_key(), world.org_a
    try:
        proxied = make_conninfo(world.dsn, host="127.0.0.1", port=str(proxy.port),
                                sslmode="disable", gssencmode="disable")
        with make_client(world, monkeypatch, dsn=proxied) as client:
            unknown = post(client, world, "engineer_a", org, key)
    finally:
        proxy.close()
    assert proxy.commit_seen.is_set()
    assert_error(unknown, 500, KEYED_UNKNOWN)
    deadline = time.monotonic() + WAIT_SECONDS
    while world.admin.execute("SELECT count(*) FROM pg_stat_activity WHERE client_port = ANY(%s)",
                              (proxy.server_side_ports,)).fetchone()[0]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    cases, recs = state(world)
    assert (len(cases), len(recs)) == ((1, 1) if persisted else (0, 0))
    with make_client(world, monkeypatch) as client:
        retry = post(client, world, "engineer_a", org, key)
    if persisted:
        assert retry.status_code == 200 and retry.json()["id"] == str(recs[0][6])   # replay of the committed case
    else:
        assert retry.status_code == 201                                             # first attempt rolled back
    assert len(state(world)[0]) == 1 and len(records(world)) == 1
    assert_no_partial_pairs(world)


# =============================================================================
# Leakage
# =============================================================================

def test_keys_and_fingerprints_never_appear_in_logs_or_responses(api, world, pool, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    key, org = new_key(), world.org_a
    fingerprint = case_create_fingerprint(org, TITLE, DESCRIPTION)
    responses = [post(api, world, "engineer_a", org, key), post(api, world, "engineer_a", org, key),
                 post(api, world, "engineer_a", org, key, description="Changed"),
                 post(api, world, "engineer_a", world.org_b, key)]
    with make_client(world, monkeypatch, wait_ms=200) as impatient:
        gate, first, _ = hold_create_at_insert(world, pool, api, "engineer_a", org, key + "-x")
        try:
            in_flight = post(impatient, world, "engineer_a", org, key + "-x")
        finally:
            gate.execute("COMMIT")
            held = result_of(first)
        responses += [in_flight, held]
    assert [r.status_code for r in responses] == [201, 200, 422, 403, 503, 201]
    for response in responses:
        assert key not in response.text and fingerprint not in response.text
        assert all(key not in v and fingerprint not in v for k, v in response.headers.items()
                   if k.lower() != "location")
    assert key not in caplog.text and fingerprint not in caplog.text
    assert world.creds["engineer_a"] not in caplog.text
