"""HTTP integration tests for the local case API (``src/api/``).

These are **HTTP tests**: requests go through FastAPI's ``TestClient`` into the
real application (routing, request parsing, exception handlers), the trusted
``operations_from_environment`` factory, the real credential resolver, the
authorized case operations, and PostgreSQL. They are distinct from the
Python-layer tests in ``test_case_authorization.py``.

Offline tests (no marker) cover what is decided before the database is
reached: header syntax, request syntax, idempotency rejection, unsupported
routes, startup configuration, and loopback binding. Tests marked ``db`` need
TEST_DATABASE_URL pointing to ``eaiso_test`` (same safety guards as the
authorization suite); each builds isolated fictional customers, actors,
grants, credentials generated at test time (stored in a temporary file), and
cases, and removes them afterwards.
"""

# Fixtures are imported from the centralized-authorization suite and used as
# test parameters, which ruff reports as unused imports and redefinitions.
# ruff: noqa: F401, F811

from __future__ import annotations

import logging
import time
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_authorization_readiness import CommitCuttingProxy
from test_case_authorization import (
    UNREACHABLE_DSN,
    WAIT_SECONDS,
    authz_dsn,
    pool,
    result_of,
    wait_blocked_by,
    world,
)

from src import credential_auth as auth
from src.api.app import create_app
from src.authorized_case_operations import OperationsConfigurationError

UNAUTHENTICATED = {"error": {"code": "unauthenticated", "message": "Authentication required"}}
NOT_FOUND = {"error": {"code": "case_not_found", "message": "Case not found"}}
ORG_DENIED = {"error": {"code": "organization_access_denied",
                        "message": "Not permitted for this organization"}}
INACTIVE = {"error": {"code": "actor_inactive", "message": "Actor is not active"}}
IDEMPOTENCY = {"error": {"code": "idempotency_not_implemented",
                         "message": "Keyed case creation (Idempotency-Key) is not implemented; "
                                    "no case was created"}}


def bearer(credential: str) -> dict:
    return {"Authorization": f"Bearer {credential}"}


def body_of(response):
    """The JSON body without the per-request ``request_id`` (5xx only)."""
    data = response.json()
    if "error" in data:
        data = {"error": {k: v for k, v in data["error"].items() if k != "request_id"}}
    return data


def assert_error(response, status, expected_body):
    assert response.status_code == status, (response.status_code, response.text)
    assert body_of(response) == expected_body
    assert response.headers["content-type"] == "application/json"
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
    if status >= 500:
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def comparable_headers(response):
    return {k: v for k, v in response.headers.items() if k.lower() not in ("x-request-id",)}


# =============================================================================
# Offline: decided before the database is reached
# =============================================================================

@pytest.fixture
def offline_client(tmp_path, monkeypatch):
    store = tmp_path / "credentials.json"
    auth.write_store(store, [])
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DSN)
    monkeypatch.setenv("EAISO_CREDENTIALS_FILE", str(store))
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


JSON = {"Content-Type": "application/json"}
SOME_TOKEN = {"Authorization": "Bearer " + "eaiso_test_" + "A" * 43}  # well-formed, never issued


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Basic dXNlcjpwYXNz"},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer "},
    {"Authorization": "Bearer a b"},
    {"Authorization": "Bearer a, Bearer b"},
    {"Authorization": "Token abc"},
    {"Authorization": " Bearer abc"},
    {"X-Actor-Id": str(uuid4()), "X-User-Id": str(uuid4()), "X-Org-Id": str(uuid4())},
])
def test_missing_or_malformed_authorization_is_generic_401_offline(offline_client, headers):
    for method, url in (("get", "/cases/" + str(uuid4())), ("get", f"/cases?organization_id={uuid4()}"),
                        ("post", "/cases")):
        response = offline_client.request(method, url, headers={**headers, **JSON},
                                          json={"organization_id": str(uuid4()), "title": "t",
                                                "description": "d"})
        assert_error(response, 401, UNAUTHENTICATED)


def test_duplicate_authorization_headers_are_rejected_offline(offline_client):
    response = offline_client.get(f"/cases?organization_id={uuid4()}",
                                  headers=[("Authorization", SOME_TOKEN["Authorization"]),
                                           ("Authorization", SOME_TOKEN["Authorization"])])
    assert_error(response, 401, UNAUTHENTICATED)


def test_unknown_credential_is_the_same_401_offline(offline_client):
    response = offline_client.get("/cases/" + str(uuid4()), headers=SOME_TOKEN)
    assert_error(response, 401, UNAUTHENTICATED)
    malformed_token = offline_client.get("/cases/" + str(uuid4()), headers=bearer("abc"))
    assert_error(malformed_token, 401, UNAUTHENTICATED)
    assert comparable_headers(response) == comparable_headers(malformed_token)


@pytest.mark.parametrize("key", ['"synthetic-case-create-0001"', "unquoted-key-value-0001", "", "x" * 300])
def test_any_idempotency_key_is_rejected_with_501_offline(offline_client, key, caplog):
    caplog.set_level(logging.DEBUG)
    response = offline_client.post("/cases", headers={"Idempotency-Key": key, **SOME_TOKEN, **JSON},
                                   json={"organization_id": str(uuid4()), "title": "t", "description": "d"})
    assert_error(response, 501, IDEMPOTENCY)
    if key:
        assert key not in response.text and key not in caplog.text


def test_idempotency_rejection_precedes_authentication_offline(offline_client):
    response = offline_client.post("/cases", headers={"Idempotency-Key": '"k-0000000000000001"'},
                                   content=b"not json")
    assert_error(response, 501, IDEMPOTENCY)


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded",
                                          "application/json; charset=latin-1", "application/jsonx"])
def test_invalid_content_type_is_415_offline(offline_client, content_type):
    headers = dict(SOME_TOKEN)
    if content_type:
        headers["Content-Type"] = content_type
    response = offline_client.post("/cases", headers=headers,
                                   content=b'{"organization_id": "x", "title": "t", "description": "d"}')
    assert_error(response, 415, {"error": {"code": "unsupported_media_type",
                                           "message": "Content-Type must be application/json"}})


@pytest.mark.parametrize("raw", [b"", b"{", b"not json", b'{"title": "t", "title": "u"}', b"\xff\xfe"])
def test_malformed_json_is_400_offline(offline_client, raw):
    response = offline_client.post("/cases", headers={**SOME_TOKEN, **JSON}, content=raw)
    assert_error(response, 400, {"error": {"code": "malformed_json",
                                           "message": "Request body is not valid JSON"}})


@pytest.mark.parametrize("payload,fields", [
    ({"title": "t", "description": "d"}, [("organization_id", "missing")]),
    ({"organization_id": "x", "title": "t"}, [("description", "missing")]),
    ({"organization_id": "x", "title": "t", "description": "d", "actor_id": "y"}, [(None, "extra_forbidden")]),
    ({"organization_id": "x", "title": "t", "description": "d", "status": "CLOSED"}, [(None, "extra_forbidden")]),
    ({"organization_id": "x", "title": "t", "description": "d", "version": 7}, [(None, "extra_forbidden")]),
    ({"organization_id": "x", "title": "t", "description": "d", "created_at": "2026-01-01T00:00:00Z"},
     [(None, "extra_forbidden")]),
    ({"organization_id": "x", "title": 5, "description": "d"}, [("title", "string_type")]),
])
def test_missing_extra_or_mistyped_body_fields_are_422_offline(offline_client, payload, fields):
    response = offline_client.post("/cases", headers={**SOME_TOKEN, **JSON}, json=payload)
    assert response.status_code == 422
    assert body_of(response) == {"error": {
        "code": "invalid_request", "message": "Request body is invalid",
        "details": [{"field": f, "problem": p} for f, p in fields]}}
    for value in ("actor_id", "CLOSED", "2026-01-01"):
        assert value not in response.text  # never echoes client names or values


@pytest.mark.parametrize("raw", [b"[" * 3000, b"[" * 60000, b'{"a":' * 10000])
def test_deeply_nested_json_is_400_not_500_offline(offline_client, raw):
    """HTTP review regression: RecursionError from the JSON parser was an anonymous 500."""
    response = offline_client.post("/cases", headers={**SOME_TOKEN, **JSON}, content=raw)
    assert_error(response, 400, {"error": {"code": "malformed_json",
                                           "message": "Request body is not valid JSON"}})


def test_trailing_slash_is_not_redirected_offline(offline_client):
    """HTTP review regression: slash redirects built Location from the client's Host header."""
    response = offline_client.get(f"/cases/?organization_id={uuid4()}", headers={**SOME_TOKEN, "Host": "evil.example"},
                                  follow_redirects=False)
    assert response.status_code == 404 and "location" not in response.headers
    assert body_of(response)["error"]["code"] == "route_not_found"


def test_idempotency_key_header_name_variants_are_rejected_offline(offline_client):
    """HTTP review regression: an underscore variant reached the create path."""
    for name in ("Idempotency_Key", "IDEMPOTENCY_KEY", "idempotency-KEY"):
        response = offline_client.post("/cases", headers={name: '"k-0000000000000001"', **SOME_TOKEN, **JSON},
                                       json={"organization_id": str(uuid4()), "title": "t", "description": "d"})
        assert_error(response, 501, IDEMPOTENCY)


def test_non_object_and_oversized_bodies_offline(offline_client):
    array = offline_client.post("/cases", headers={**SOME_TOKEN, **JSON}, content=b"[1, 2]")
    assert_error(array, 422, {"error": {"code": "invalid_request",
                                        "message": "Request body must be a JSON object"}})
    big = offline_client.post("/cases", headers={**SOME_TOKEN, **JSON},
                              json={"organization_id": "x", "title": "t", "description": "d" * 70_000})
    assert_error(big, 413, {"error": {"code": "payload_too_large", "message": "Request body is too large"}})


@pytest.mark.parametrize("url,code", [
    ("/cases", "missing_parameter"),
    ("/cases?limit=5", "missing_parameter"),
    (f"/cases?organization_id={uuid4()}&cursor=abc", "cursor_not_supported"),
    (f"/cases?cursor=abc&organization_id={uuid4()}", "cursor_not_supported"),
    (f"/cases?organization={uuid4()}", "unknown_parameter"),
    (f"/cases?organization_id={uuid4()}&organization_id={uuid4()}", "duplicate_parameter"),
    (f"/cases?organization_id={uuid4()}&limit=abc", "invalid_parameter"),
    (f"/cases?organization_id={uuid4()}&limit=-1", "invalid_parameter"),
    (f"/cases?organization_id={uuid4()}&limit=1.5", "invalid_parameter"),
    (f"/cases/{uuid4()}?expand=all", "unknown_parameter"),
    ("/cases?all=true", "unknown_parameter"),
])
def test_query_parameter_errors_are_400_offline(offline_client, url, code):
    response = offline_client.get(url, headers=SOME_TOKEN)
    assert response.status_code == 400
    assert body_of(response)["error"]["code"] == code
    post = offline_client.post("/cases?dry_run=1", headers={**SOME_TOKEN, **JSON},
                               json={"organization_id": "x", "title": "t", "description": "d"})
    assert body_of(post)["error"]["code"] == "unknown_parameter"


@pytest.mark.parametrize("method,url", [
    ("patch", "/cases/{id}"), ("put", "/cases/{id}"), ("delete", "/cases/{id}"),
    ("post", "/cases/{id}"), ("post", "/cases/{id}/transitions"), ("post", "/cases/{id}/status"),
    ("get", "/admin"), ("post", "/credentials"), ("post", "/actors"), ("post", "/memberships"),
    ("get", "/docs"), ("get", "/redoc"), ("get", "/openapi.json"), ("delete", "/cases"),
])
def test_no_transition_administrative_or_documentation_routes_offline(offline_client, method, url):
    response = offline_client.request(method, url.format(id=uuid4()), headers=SOME_TOKEN)
    assert response.status_code in (404, 405)
    assert body_of(response)["error"]["code"] in ("route_not_found", "method_not_allowed")


def test_request_ids_are_server_generated_offline(offline_client):
    first = offline_client.get("/cases", headers={**SOME_TOKEN, "X-Request-ID": "client-chosen"})
    second = offline_client.get("/cases", headers=SOME_TOKEN)
    assert first.headers["x-request-id"] != "client-chosen"
    assert len(first.headers["x-request-id"]) == 32 and first.headers["x-request-id"] != second.headers["x-request-id"]


@pytest.mark.parametrize("dsn", [None, "", "   ", "not a connection string password=hunter2-secret",
                                 "postgresql://user:hunter2-secret@127.0.0.1:5432"])
def test_startup_fails_cleanly_without_valid_database_configuration_offline(tmp_path, monkeypatch, dsn):
    store = tmp_path / "credentials.json"
    auth.write_store(store, [])
    monkeypatch.setenv("EAISO_CREDENTIALS_FILE", str(store))
    if dsn is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", dsn)
    with pytest.raises(OperationsConfigurationError) as info, TestClient(create_app()):
        pass
    assert "hunter2" not in str(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_startup_fails_without_a_readable_credential_store_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DSN)
    monkeypatch.setenv("EAISO_CREDENTIALS_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(auth.CredentialConfigurationError), TestClient(create_app()):
        pass


def test_local_runner_binds_loopback_only_offline(monkeypatch):
    import uvicorn

    from scripts import run_local_api

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    assert run_local_api.main(["--port", "8000"]) == 0
    (args, kwargs), = calls
    assert args == ("src.api.app:app",) and kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8000
    with pytest.raises(SystemExit):
        run_local_api.main(["--host", "0.0.0.0"])  # no such option


# =============================================================================
# PostgreSQL-backed HTTP tests
# =============================================================================

@pytest.fixture
def api(world, monkeypatch):
    """The real application, configured only through server environment variables."""
    monkeypatch.setenv("DATABASE_URL", world.dsn)        # guarded: eaiso_test only
    monkeypatch.setenv("EAISO_CREDENTIALS_FILE", str(world.store))
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


def headers_for(world, key, **extra):
    return {**bearer(world.creds[key]), **JSON, **extra}


def create(api, world, key, org, title="Synthetic HTTP case", description="Fictional HTTP detail",
           **extra_headers):
    return api.post("/cases", headers=headers_for(world, key, **extra_headers),
                    json={"organization_id": str(org), "title": title, "description": description})


def db_case(world, case_id):
    return world.admin.execute(
        "SELECT id, organization_id, title, description, status, version, created_at, updated_at "
        "FROM case_record WHERE id = %s", (case_id,)).fetchone()


def assert_matches_db(world, payload):
    row = db_case(world, payload["id"])
    assert row is not None
    assert (str(row[0]), str(row[1]), row[2], row[3], row[4], row[5]) == (
        payload["id"], payload["organization_id"], payload["title"], payload["description"],
        payload["status"], payload["version"])
    assert datetime.fromisoformat(payload["created_at"]) == row[6]
    assert datetime.fromisoformat(payload["updated_at"]) == row[7]


# ----- successful operations ----------------------------------------------------

@pytest.mark.db
def test_v01_v03_engineer_creates_reads_and_lists_in_a(api, world):
    b_case = world.seed_case(world.org_b)
    count_before = world.snapshot()[1]
    created = create(api, world, "engineer_a", world.org_a)
    assert created.status_code == 201
    payload = created.json()
    assert created.headers["location"] == f"/cases/{payload['id']}"
    assert payload["organization_id"] == str(world.org_a)
    assert (payload["status"], payload["version"]) == ("NEW", 1)
    assert set(payload) == {"id", "organization_id", "title", "description", "status", "version",
                            "created_at", "updated_at"}
    assert_matches_db(world, payload)
    assert world.snapshot()[1] == count_before + 1

    read = api.get(created.headers["location"], headers=bearer(world.creds["engineer_a"]))
    assert read.status_code == 200 and read.json() == payload

    listed = api.get(f"/cases?organization_id={world.org_a}", headers=bearer(world.creds["engineer_a"]))
    assert listed.status_code == 200
    assert listed.json() == {"items": [{k: v for k, v in payload.items()
                                        if k not in ("description", "updated_at")}],
                             "has_more": False}
    assert str(b_case) not in listed.text


@pytest.mark.db
def test_v04_manager_creates_and_lists_in_its_customer(api, world):
    created = create(api, world, "manager_a", world.org_a)
    assert created.status_code == 201
    assert_matches_db(world, created.json())
    listed = api.get(f"/cases?organization_id={world.org_a}", headers=bearer(world.creds["manager_a"]))
    assert [i["id"] for i in listed.json()["items"]] == [created.json()["id"]]


@pytest.mark.db
def test_v05_v07_requester_creates_but_cannot_read_or_list(api, world):
    other = world.seed_case(world.org_a)
    created = create(api, world, "requester_a", world.org_a)
    assert created.status_code == 201
    assert_matches_db(world, created.json())
    cred = bearer(world.creds["requester_a"])
    own = api.get(f"/cases/{created.json()['id']}", headers=cred)
    foreign = api.get(f"/cases/{other}", headers=cred)
    assert_error(own, 404, NOT_FOUND)
    assert_error(foreign, 404, NOT_FOUND)
    assert own.content == foreign.content
    assert_error(api.get(f"/cases?organization_id={world.org_a}", headers=cred), 403, ORG_DENIED)


@pytest.mark.db
def test_multiple_grants_are_independent_and_membership_revocation_applies_to_next_request(api, world):
    cred = bearer(world.creds["engineer_ab"])
    in_a = create(api, world, "engineer_ab", world.org_a).json()
    in_b = create(api, world, "engineer_ab", world.org_b).json()
    for org, case in ((world.org_a, in_a), (world.org_b, in_b)):
        listed = api.get(f"/cases?organization_id={org}", headers=cred).json()
        assert [i["id"] for i in listed["items"]] == [case["id"]]
    world.prov.deactivate_grant(world.admin, world.actors["engineer_ab"], world.org_b)
    before = world.snapshot()
    assert_error(api.get(f"/cases/{in_b['id']}", headers=cred), 404, NOT_FOUND)
    assert_error(api.get(f"/cases?organization_id={world.org_b}", headers=cred), 403, ORG_DENIED)
    assert_error(create(api, world, "engineer_ab", world.org_b), 403, ORG_DENIED)
    assert world.snapshot() == before
    assert api.get(f"/cases/{in_a['id']}", headers=cred).status_code == 200


@pytest.mark.db
def test_list_is_bounded_ordered_and_reports_has_more(api, world):
    ids = [create(api, world, "engineer_a", world.org_a, title=f"Synthetic {i}").json()["id"]
           for i in range(3)]
    expected = [str(r[0]) for r in world.admin.execute(
        "SELECT id FROM case_record WHERE organization_id = %s ORDER BY created_at, id",
        (world.org_a,)).fetchall()]
    assert sorted(expected) == sorted(ids)
    cred = bearer(world.creds["engineer_a"])
    page = api.get(f"/cases?organization_id={world.org_a}&limit=2", headers=cred).json()
    assert [i["id"] for i in page["items"]] == expected[:2] and page["has_more"] is True
    full = api.get(f"/cases?organization_id={world.org_a}&limit=100", headers=cred).json()
    assert [i["id"] for i in full["items"]] == expected and full["has_more"] is False
    assert all("description" not in i for i in full["items"])


@pytest.mark.db
def test_two_identical_unkeyed_creates_produce_distinct_cases(api, world):
    first, second = (create(api, world, "engineer_a", world.org_a, title="Same", description="Same")
                     for _ in range(2))
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert world.admin.execute("SELECT count(*) FROM case_record WHERE organization_id = %s "
                               "AND title = 'Same'", (world.org_a,)).fetchone()[0] == 2


# ----- tenant isolation ---------------------------------------------------------

@pytest.mark.db
def test_v08_v11_engineer_of_a_cannot_create_read_or_list_b(api, world):
    b_case = world.seed_case(world.org_b)
    before = world.snapshot()
    cred = bearer(world.creds["engineer_a"])
    assert_error(create(api, world, "engineer_a", world.org_b), 403, ORG_DENIED)
    foreign = api.get(f"/cases/{b_case}", headers=cred)
    missing = api.get(f"/cases/{uuid4()}", headers=cred)
    assert_error(foreign, 404, NOT_FOUND)
    assert_error(missing, 404, NOT_FOUND)
    assert foreign.content == missing.content
    assert comparable_headers(foreign) == comparable_headers(missing)
    assert_error(api.get(f"/cases?organization_id={world.org_b}", headers=cred), 403, ORG_DENIED)
    assert world.snapshot() == before


@pytest.mark.db
def test_v15_v17_provider_without_grants_has_no_case_access(api, world):
    a_case = world.seed_case(world.org_a)
    before = world.snapshot()
    cred = bearer(world.creds["platform_admin"])
    assert_error(api.get(f"/cases?organization_id={world.org_a}", headers=cred), 403, ORG_DENIED)
    assert_error(api.get(f"/cases/{a_case}", headers=cred), 404, NOT_FOUND)
    assert_error(create(api, world, "platform_admin", world.org_a), 403, ORG_DENIED)
    assert world.snapshot() == before


@pytest.mark.db
def test_s02_nonexistent_organization_is_identical_to_forbidden(api, world):
    before = world.snapshot()
    nonexistent = create(api, world, "engineer_a", uuid4())
    forbidden = create(api, world, "engineer_a", world.org_b)
    assert_error(nonexistent, 403, ORG_DENIED)
    assert nonexistent.content == forbidden.content
    assert world.snapshot() == before


# ----- authentication and revocation --------------------------------------------

@pytest.mark.db
def test_s01_every_unauthenticated_reason_gets_the_identical_401(api, world):
    # Revoked credential, and a credential whose actor row no longer exists.
    revoked_cred = world.creds["manager_a"]
    entry = next(e for e in auth.read_store(world.store) if e.actor_id == world.actors["manager_a"])
    auth.revoke_credential(world.store, entry.credential_id)
    ghost = world.prov.create_actor(world.admin, "synthetic deleted actor", world.prov.PROVIDER)
    ghost_cred, _ = auth.issue_credential(world.store, ghost)
    world.admin.execute("DELETE FROM actor WHERE id = %s", (ghost,))
    before = world.snapshot()
    variants = {
        "missing": {},
        "malformed header": {"Authorization": "Basic abc"},
        "malformed token": bearer("not-a-credential"),
        "unknown": bearer(auth.generate_credential()),
        "revoked": bearer(revoked_cred),
        "actor_not_found": bearer(ghost_cred),
    }
    responses = {}
    for label, headers in variants.items():
        response = api.post("/cases", headers={**headers, **JSON},
                            json={"organization_id": str(world.org_a), "title": "t", "description": "d"})
        assert_error(response, 401, UNAUTHENTICATED)
        responses[label] = (response.content, comparable_headers(response))
    assert len({(c, tuple(sorted(h.items()))) for c, h in responses.values()}) == 1
    assert world.snapshot() == before


@pytest.mark.db
def test_duplicate_authorization_headers_with_valid_credentials_never_authenticate(api, world):
    a_case = world.seed_case(world.org_a)
    for pair in ((world.creds["engineer_a"], world.creds["engineer_a"]),
                 (world.creds["engineer_a"], world.creds["engineer_b"]),
                 (world.creds["engineer_b"], world.creds["engineer_a"])):
        response = api.get(f"/cases/{a_case}", headers=[("Authorization", f"Bearer {pair[0]}"),
                                                       ("Authorization", f"Bearer {pair[1]}")])
        assert_error(response, 401, UNAUTHENTICATED)


@pytest.mark.db
def test_v14_inactive_actor_is_403_without_insert(api, world):
    world.admin.execute("UPDATE actor SET is_active = FALSE WHERE id = %s", (world.actors["engineer_a"],))
    before = world.snapshot()
    assert_error(create(api, world, "engineer_a", world.org_a), 403, INACTIVE)
    assert world.snapshot() == before


@pytest.mark.db
def test_v13_inactive_membership_denies(api, world):
    a_case = world.seed_case(world.org_a)
    world.prov.deactivate_grant(world.admin, world.actors["engineer_a"], world.org_a)
    before = world.snapshot()
    cred = bearer(world.creds["engineer_a"])
    assert_error(api.get(f"/cases/{a_case}", headers=cred), 404, NOT_FOUND)
    assert_error(api.get(f"/cases?organization_id={world.org_a}", headers=cred), 403, ORG_DENIED)
    assert_error(create(api, world, "engineer_a", world.org_a), 403, ORG_DENIED)
    assert world.snapshot() == before


@pytest.mark.db
def test_v19_v20_s03_spoofed_identity_headers_and_body_fields_never_override_the_credential(api, world):
    b_case = world.seed_case(world.org_b)
    spoof = {"X-Actor-Id": str(world.actors["engineer_b"]), "X-User-Id": str(world.actors["engineer_b"]),
             "X-Org-Id": str(world.org_b)}
    # Invalid credential plus a header naming a real engineer: still 401.
    assert_error(api.get(f"/cases?organization_id={world.org_b}",
                         headers={**bearer(auth.generate_credential()), **spoof}), 401, UNAUTHENTICATED)
    # Valid engineer-of-A credential plus headers pointing at B: identity unchanged.
    cred = {**bearer(world.creds["engineer_a"]), **spoof}
    assert_error(api.get(f"/cases/{b_case}", headers=cred), 404, NOT_FOUND)
    assert_error(api.get(f"/cases?organization_id={world.org_b}", headers=cred), 403, ORG_DENIED)
    assert api.get(f"/cases?organization_id={world.org_a}", headers=cred).status_code == 200
    created = create(api, world, "engineer_a", world.org_a, **spoof)
    assert created.status_code == 201 and created.json()["organization_id"] == str(world.org_a)
    # Privileged body fields are rejected, not applied.
    before = world.snapshot()
    for extra in ({"actor_id": str(world.actors["engineer_b"])}, {"status": "CLOSED"}, {"version": 9}):
        response = api.post("/cases", headers=headers_for(world, "engineer_a"),
                            json={"organization_id": str(world.org_a), "title": "t", "description": "d", **extra})
        assert response.status_code == 422 and body_of(response)["error"]["code"] == "invalid_request"
    assert world.snapshot() == before


@pytest.mark.db
def test_credential_rotation_disables_old_credential_and_keeps_actor(api, world):
    created = create(api, world, "engineer_a", world.org_a).json()
    entry = next(e for e in auth.read_store(world.store) if e.actor_id == world.actors["engineer_a"])
    new_cred, new_entry = auth.rotate_credential(world.store, entry.credential_id)
    assert new_entry.actor_id == entry.actor_id
    assert_error(api.get(f"/cases/{created['id']}", headers=bearer(world.creds["engineer_a"])),
                 401, UNAUTHENTICATED)
    assert api.get(f"/cases/{created['id']}", headers=bearer(new_cred)).json() == created


@pytest.mark.db
def test_s07_revocation_committing_first_makes_waiting_http_create_deny(api, world, pool):
    actor, org = world.actors["engineer_a"], world.org_a
    holder = world.conn()
    before = world.snapshot()
    with holder.transaction():
        holder.execute("UPDATE organization_membership SET is_active = FALSE "
                       "WHERE actor_id = %s AND organization_id = %s", (actor, org))
        future = pool.submit(create, api, world, "engineer_a", org)
        wait_blocked_by(world.admin, holder.info.backend_pid, "FROM organization_membership", future)
    response = result_of(future)
    assert_error(response, 403, ORG_DENIED)
    assert world.snapshot() == before


# ----- validation ordering against real credentials -----------------------------

@pytest.mark.db
def test_validation_and_authentication_ordering(api, world):
    """Header syntax, then request syntax, then (in the layer) authentication, then values."""
    unknown = bearer(auth.generate_credential())
    valid = bearer(world.creds["engineer_a"])
    before = world.snapshot()
    # Request-syntax errors are reported before the credential is verified.
    assert api.post("/cases", headers={**unknown, **JSON}, content=b"{").status_code == 400
    assert api.post("/cases", headers={**unknown, **JSON}, json={"title": "t"}).status_code == 422
    assert api.get("/cases", headers=unknown).status_code == 400            # missing organization_id
    # Missing or malformed Authorization is reported before request syntax.
    assert api.post("/cases", headers=JSON, content=b"{").status_code == 401
    # Value checks run in the layer after authentication: invalid credential -> 401 ...
    for call in (lambda h: api.post("/cases", headers={**h, **JSON},
                                    json={"organization_id": str(world.org_a), "title": " ", "description": "d"}),
                 lambda h: api.get("/cases/not-a-uuid", headers=h),
                 lambda h: api.get("/cases?organization_id=not-a-uuid", headers=h),
                 lambda h: api.get(f"/cases?organization_id={world.org_a}&limit=0", headers=h)):
        assert_error(call(unknown), 401, UNAUTHENTICATED)
    # ... and with a valid credential, the value error.
    blank = api.post("/cases", headers={**valid, **JSON},
                     json={"organization_id": str(world.org_a), "title": " ", "description": "d"})
    assert_error(blank, 422, {"error": {"code": "invalid_request", "message": "title must be non-blank text"}})
    bad_body_org = api.post("/cases", headers={**valid, **JSON},
                            json={"organization_id": "not-a-uuid", "title": "t", "description": "d"})
    assert_error(bad_body_org, 422, {"error": {"code": "invalid_request",
                                               "message": "organization_id must be a canonical UUID"}})
    assert_error(api.get("/cases/not-a-uuid", headers=valid), 400,
                 {"error": {"code": "invalid_parameter", "message": "Case ID must be a canonical UUID"}})
    assert_error(api.get(f"/cases?organization_id={str(world.org_a).upper().replace('-', '')}", headers=valid),
                 400, {"error": {"code": "invalid_parameter",
                                 "message": "organization_id must be a canonical UUID"}})
    for limit in ("0", "101"):
        assert_error(api.get(f"/cases?organization_id={world.org_a}&limit={limit}", headers=valid), 400,
                     {"error": {"code": "invalid_parameter", "message": "limit must be between 1 and 100"}})
    assert world.snapshot() == before


@pytest.mark.db
def test_idempotency_key_never_creates_a_case(api, world, caplog):
    caplog.set_level(logging.DEBUG)
    before = world.snapshot()
    key = '"synthetic-case-create-' + uuid4().hex + '"'
    response = create(api, world, "engineer_a", world.org_a, **{"Idempotency-Key": key})
    assert_error(response, 501, IDEMPOTENCY)
    assert world.snapshot() == before
    assert key.strip('"') not in caplog.text and key.strip('"') not in response.text


# ----- failure behavior ---------------------------------------------------------

@pytest.mark.db
def test_s05_database_outage_is_generic_500_without_writes(api, world):
    a_case = world.seed_case(world.org_a)
    before = world.snapshot()
    ops = api.app.state.case_operations
    cred = bearer(world.creds["engineer_a"])
    real_dsn = ops._dsn
    ops._dsn = UNREACHABLE_DSN          # the case database is down; identity lookups still work
    try:
        layer_down = [create(api, world, "engineer_a", world.org_a),
                      api.get(f"/cases/{a_case}", headers=cred),
                      api.get(f"/cases?organization_id={world.org_a}", headers=cred)]
    finally:
        ops._dsn = real_dsn
    lookup = ops._resolver._actor_lookup
    real_lookup_dsn = lookup.dsn
    lookup.dsn = UNREACHABLE_DSN        # the resolver's actor lookup is down
    try:
        resolver_down = [api.get(f"/cases/{a_case}", headers=cred)]
    finally:
        lookup.dsn = real_lookup_dsn
    for response in layer_down + resolver_down:
        assert_error(response, 500, {"error": {"code": "internal_error", "message": "Internal server error"}})
    assert world.snapshot() == before


@pytest.mark.db
def test_credential_store_failure_fails_closed(api, world):
    original = world.store.read_bytes()
    before = world.snapshot()
    world.store.write_text("{ damaged", encoding="utf-8")
    try:
        response = create(api, world, "engineer_a", world.org_a)
    finally:
        world.store.write_bytes(original)
    assert_error(response, 500, {"error": {"code": "internal_error", "message": "Internal server error"}})
    assert world.snapshot() == before
    assert create(api, world, "engineer_a", world.org_a).status_code == 201  # recovers after repair


@pytest.mark.db
@pytest.mark.parametrize("mode,persisted", [("before_server", False), ("after_server", True)])
def test_unknown_commit_outcome_is_never_201(world, monkeypatch, mode, persisted):
    """Real connection loss at COMMIT, through a TCP proxy in front of PostgreSQL."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(world.dsn)
    proxy = CommitCuttingProxy(params.get("host") or "127.0.0.1", int(params.get("port") or 5432), mode)
    title = f"Synthetic HTTP commit-cut {mode} {uuid4().hex}"
    try:
        monkeypatch.setenv("DATABASE_URL", make_conninfo(world.dsn, host="127.0.0.1", port=str(proxy.port),
                                                          sslmode="disable", gssencmode="disable"))
        monkeypatch.setenv("EAISO_CREDENTIALS_FILE", str(world.store))
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = create(client, world, "engineer_a", world.org_a, title=title)
    finally:
        proxy.close()
    assert proxy.commit_seen.is_set()
    assert_error(response, 500, {"error": {
        "code": "case_creation_outcome_unknown",
        "message": "The case may or may not have been created. Retrying this request may create "
                   "a duplicate case."}})
    assert "location" not in response.headers
    deadline = time.monotonic() + WAIT_SECONDS
    while world.admin.execute("SELECT count(*) FROM pg_stat_activity WHERE client_port = ANY(%s)",
                              (proxy.server_side_ports,)).fetchone()[0]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    rows = world.admin.execute("SELECT organization_id FROM case_record WHERE title = %s", (title,)).fetchall()
    assert rows == ([(world.org_a,)] if persisted else [])


@pytest.mark.db
def test_no_response_or_log_leaks_secrets_case_text_or_sql(api, world, caplog):
    caplog.set_level(logging.DEBUG)
    secret_title = "Synthetic private title " + uuid4().hex
    secret_description = "Synthetic private description " + uuid4().hex
    b_case = world.seed_case(world.org_b, title=secret_title)
    cred_a, cred_b = world.creds["engineer_a"], world.creds["engineer_b"]
    revoked = world.creds["manager_a"]
    entry = next(e for e in auth.read_store(world.store) if e.actor_id == world.actors["manager_a"])
    auth.revoke_credential(world.store, entry.credential_id)
    responses = [
        api.get(f"/cases/{b_case}", headers=bearer(cred_a)),
        api.get(f"/cases?organization_id={world.org_b}", headers=bearer(cred_a)),
        api.post("/cases", headers={**bearer(cred_a), **JSON},
                 json={"organization_id": str(world.org_b), "title": secret_title, "description": secret_description}),
        api.post("/cases", headers={**bearer(cred_a), **JSON},
                 json={"organization_id": str(world.org_a), "title": secret_title, "description": " "}),
        api.post("/cases", headers={**bearer(cred_a), **JSON},
                 json={"organization_id": str(world.org_a), "title": secret_title,
                       "description": secret_description, "actor_id": cred_b}),
        api.get(f"/cases/{b_case}", headers=bearer(revoked)),
        api.get(f"/cases/{b_case}", headers=[("Authorization", f"Bearer {cred_a}"),
                                             ("Authorization", f"Bearer {cred_b}")]),
    ]
    ops = api.app.state.case_operations
    real_dsn = ops._dsn
    ops._dsn = UNREACHABLE_DSN
    try:
        responses.append(api.post("/cases", headers={**bearer(cred_a), **JSON},
                                  json={"organization_id": str(world.org_a), "title": secret_title,
                                        "description": secret_description}))
    finally:
        ops._dsn = real_dsn
    assert all(r.status_code >= 400 for r in responses)
    forbidden = (cred_a, cred_b, revoked, secret_title, secret_description, "SELECT", "INSERT",
                 "case_record", "organization_membership", "psycopg", "Traceback", world.dsn)
    for response in responses:
        for value in forbidden:
            assert value not in response.text, value
            assert all(value not in v for v in response.headers.values()), value
    for value in (cred_a, cred_b, revoked, secret_title, secret_description, world.dsn):
        assert value not in caplog.text, "log leaked a secret or case text"
    assert "http.request method=" in caplog.text  # request logging did run
