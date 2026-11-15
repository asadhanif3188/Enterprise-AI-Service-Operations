"""Local opaque credential verification (ADR-0005): identity only, not authorization.

Offline tests use a temporary credential store and a fake actor lookup.
Tests marked ``db`` require TEST_DATABASE_URL pointing to the dedicated
``eaiso_test`` database with db/001-003 applied. Every raw credential used
here is generated at test time; none is committed.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import traceback
from uuid import UUID, uuid4

import pytest

from src import credential_auth as auth

A1 = uuid4()
A2 = uuid4()


class FakeLookup:
    """In-memory actor status: True active, False inactive, missing -> None."""

    def __init__(self, statuses=None, error=None):
        self.statuses = dict(statuses or {})
        self.error = error
        self.calls = []

    def __call__(self, actor_id):
        self.calls.append(actor_id)
        if self.error is not None:
            raise self.error
        return self.statuses.get(actor_id)


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "creds" / "test-credentials.json"


def make_resolver(path, statuses=None, error=None):
    lookup = FakeLookup(statuses if statuses is not None else {A1: True, A2: True}, error)
    return auth.CredentialResolver(auth.FileCredentialStore(path), lookup), lookup


def outcome(resolver, credential):
    try:
        return resolver.resolve(credential)
    except auth.AuthenticationError as exc:
        return exc


def assert_no_secret(texts, *secret_values):
    for text in texts:
        for value in secret_values:
            assert value not in text, "secret material leaked"


def exception_texts(exc):
    return [str(exc), repr(exc), "".join(traceback.format_exception(exc)), repr(exc.args)]


# =============================================================================
# Credential generation and format
# =============================================================================

def test_generated_credentials_have_256_random_bits_and_fixed_format():
    for _ in range(50):
        credential = auth.generate_credential()
        assert credential.startswith(auth.CREDENTIAL_PREFIX)
        body = credential[len(auth.CREDENTIAL_PREFIX):]
        assert len(body) == 43
        assert len(base64.urlsafe_b64decode(body + "=")) == 32
        assert auth.is_well_formed(credential)


def test_generation_uses_secrets_module(monkeypatch):
    seen = []
    real = auth.secrets.token_urlsafe

    def spy(nbytes):
        seen.append(nbytes)
        return real(nbytes)

    monkeypatch.setattr(auth.secrets, "token_urlsafe", spy)
    auth.generate_credential()
    assert seen == [32]


def test_generated_credentials_are_unique():
    values = {auth.generate_credential() for _ in range(5000)}
    assert len(values) == 5000


def test_digest_is_deterministic_non_reversible_form_and_distinct():
    c1, c2 = auth.generate_credential(), auth.generate_credential()
    d1 = auth.credential_digest(c1)
    assert d1 == auth.credential_digest(c1)
    assert d1 != auth.credential_digest(c2)
    assert d1.startswith("sha256:") and len(d1) == 71
    assert c1[len(auth.CREDENTIAL_PREFIX):] not in d1


def test_non_canonical_encoding_is_malformed():
    credential = auth.generate_credential()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(credential[-1])
    variant = credential[:-1] + alphabet[last | 1]  # set an unused trailing bit
    assert variant != credential
    assert not auth.is_well_formed(variant)


# =============================================================================
# Store validation (fail closed)
# =============================================================================

def entry_json(**overrides):
    data = {"credential_id": "0123456789abcdef",
            "digest": auth.credential_digest(auth.generate_credential()),
            "actor_id": str(A1), "status": "active", "created_at": "2026-11-15T00:00:00+00:00"}
    data.update(overrides)
    return data


@pytest.mark.parametrize("content", [
    "",
    "not json",
    "[]",
    json.dumps({"version": 2, "credentials": []}),
    json.dumps({"version": 1}),
    json.dumps({"version": 1, "credentials": {}}),
    json.dumps({"version": 1, "credentials": [], "extra": 1}),
    json.dumps({"version": 1, "credentials": ["x"]}),
    json.dumps({"version": 1, "credentials": [entry_json(digest="sha256:abc")]}),
    json.dumps({"version": 1, "credentials": [entry_json(digest="md5:" + "0" * 64)]}),
    json.dumps({"version": 1, "credentials": [entry_json(actor_id="not-a-uuid")]}),
    json.dumps({"version": 1, "credentials": [entry_json(status="enabled")]}),
    json.dumps({"version": 1, "credentials": [entry_json(credential_id="short")]}),
    # A raw credential must never be stored: unknown keys are rejected.
    json.dumps({"version": 1, "credentials": [entry_json(credential="eaiso_test_x")]}),
])
def test_invalid_store_is_rejected(store_path, content):
    store_path.parent.mkdir(parents=True)
    store_path.write_text(content, encoding="utf-8")
    with pytest.raises(auth.CredentialConfigurationError):
        auth.FileCredentialStore(store_path)


def test_missing_store_fails_at_startup(store_path):
    with pytest.raises(auth.CredentialConfigurationError):
        auth.resolver_from_environment("postgresql://unused@127.0.0.1/eaiso_test", store_path)


@pytest.mark.parametrize("field", ["digest", "credential_id"])
def test_duplicate_digest_or_credential_id_rejected(store_path, field):
    e1 = entry_json()
    e2 = entry_json(credential_id="fedcba9876543210")
    e2[field] = e1[field]
    store_path.parent.mkdir(parents=True)
    store_path.write_text(json.dumps({"version": 1, "credentials": [e1, e2]}), encoding="utf-8")
    with pytest.raises(auth.CredentialConfigurationError, match=f"duplicate {field}"):
        auth.FileCredentialStore(store_path)


# =============================================================================
# Resolution outcomes (offline)
# =============================================================================

def test_valid_credential_resolves_to_actor(store_path):
    credential, entry = auth.issue_credential(store_path, A1)
    resolver, lookup = make_resolver(store_path)
    verified = resolver.resolve(credential)
    assert verified == auth.VerifiedActor(A1, entry.credential_id)
    assert lookup.calls == [A1]


@pytest.mark.parametrize("value", [None, ""])
def test_missing_credential_is_401(store_path, value):
    auth.issue_credential(store_path, A1)
    resolver, lookup = make_resolver(store_path)
    result = outcome(resolver, value)
    assert isinstance(result, auth.Unauthenticated) and result.reason == "missing"
    assert result.status_equivalent == 401
    assert lookup.calls == []


def test_malformed_and_unknown_credentials_are_401(store_path):
    credential, _ = auth.issue_credential(store_path, A1)
    resolver, lookup = make_resolver(store_path)
    malformed = [
        "x", "eaiso_test_", credential[:-1], credential + "A", " " + credential, credential + "\n",
        credential.upper(), "Bearer " + credential, credential.replace("eaiso_test_", "eaiso_prod_"),
        credential.encode(), 12345, ["list"], "eaiso_test_" + "!" * 43,
    ]
    for value in malformed:
        result = outcome(resolver, value)
        assert isinstance(result, auth.Unauthenticated), value
        assert result.reason == "malformed"
    unknown = auth.generate_credential()
    result = outcome(resolver, unknown)
    assert isinstance(result, auth.Unauthenticated) and result.reason == "unknown"
    assert lookup.calls == []  # no database lookup for unverified input


def test_spoofed_identity_inputs_never_resolve(store_path):
    credential_a1, _ = auth.issue_credential(store_path, A1)
    resolver, lookup = make_resolver(store_path)
    for spoof in [str(A1), A1, "Ali (synthetic ABC service engineer)", str(uuid4()),
                  auth.credential_digest(credential_a1), {"actor_id": str(A1)},
                  "X-Actor-Id: " + str(A1)]:
        result = outcome(resolver, spoof)
        assert isinstance(result, auth.Unauthenticated), spoof
        assert result.reason == "malformed", spoof
    assert lookup.calls == []
    # The resolver accepts only the credential: no actor, organization, or header input.
    params = list(inspect.signature(auth.CredentialResolver.resolve).parameters)
    assert params == ["self", "credential"]


def test_no_cross_actor_resolution(store_path):
    c1, _ = auth.issue_credential(store_path, A1)
    c2, _ = auth.issue_credential(store_path, A2)
    resolver, _ = make_resolver(store_path)
    assert resolver.resolve(c1).actor_id == A1
    assert resolver.resolve(c2).actor_id == A2
    # Revoking one actor's credential does not affect the other's.
    auth.revoke_credential(store_path, auth.read_store(store_path)[0].credential_id)
    assert isinstance(outcome(resolver, c1), auth.Unauthenticated)
    assert resolver.resolve(c2).actor_id == A2


def test_revocation_takes_effect_without_restart(store_path):
    credential, entry = auth.issue_credential(store_path, A1)
    resolver, _ = make_resolver(store_path)
    assert resolver.resolve(credential).actor_id == A1
    auth.revoke_credential(store_path, entry.credential_id)
    result = outcome(resolver, credential)
    assert isinstance(result, auth.Unauthenticated) and result.reason == "revoked"


def test_removed_mapping_is_unknown(store_path):
    credential, entry = auth.issue_credential(store_path, A1)
    resolver, _ = make_resolver(store_path)
    auth.remove_credential(store_path, entry.credential_id)
    result = outcome(resolver, credential)
    assert isinstance(result, auth.Unauthenticated) and result.reason == "unknown"


def test_rotation_preserves_actor_and_rejects_old_credential(store_path):
    old, old_entry = auth.issue_credential(store_path, A1)
    resolver, _ = make_resolver(store_path)
    new, new_entry = auth.rotate_credential(store_path, old_entry.credential_id)
    assert new != old and new_entry.credential_id != old_entry.credential_id
    assert resolver.resolve(new).actor_id == A1
    result = outcome(resolver, old)
    assert isinstance(result, auth.Unauthenticated) and result.reason == "revoked"
    with pytest.raises(auth.CredentialConfigurationError):
        auth.rotate_credential(store_path, old_entry.credential_id)  # already revoked


def test_inactive_actor_is_403(store_path):
    credential, _ = auth.issue_credential(store_path, A1)
    resolver, _ = make_resolver(store_path, {A1: False})
    result = outcome(resolver, credential)
    assert isinstance(result, auth.InactiveActor) and result.status_equivalent == 403


def test_credential_for_missing_actor_row_is_401(store_path):
    credential, _ = auth.issue_credential(store_path, A1)
    resolver, _ = make_resolver(store_path, {})
    result = outcome(resolver, credential)
    assert isinstance(result, auth.Unauthenticated) and result.reason == "actor_not_found"


@pytest.mark.parametrize("error", [ConnectionError("db down"), TimeoutError(), RuntimeError("boom")])
def test_lookup_failure_is_generic_unavailable_never_success(store_path, error):
    credential, _ = auth.issue_credential(store_path, A1)
    resolver, _ = make_resolver(store_path, error=error)
    result = outcome(resolver, credential)
    assert isinstance(result, auth.AuthenticationUnavailable)
    assert result.reason == "database" and result.status_equivalent == 500
    # No exception chaining at all: the dependency error is unreachable from the result.
    assert result.__cause__ is None and result.__context__ is None


@pytest.mark.parametrize("damage", ["corrupt", "delete", "invalid_entry"])
def test_store_damaged_after_startup_fails_closed(store_path, damage):
    credential, _ = auth.issue_credential(store_path, A1)
    resolver, lookup = make_resolver(store_path)
    assert resolver.resolve(credential).actor_id == A1
    if damage == "corrupt":
        store_path.write_text("{", encoding="utf-8")
    elif damage == "delete":
        store_path.unlink()
    else:
        data = json.loads(store_path.read_text(encoding="utf-8"))
        data["credentials"][0]["status"] = "maybe"
        store_path.write_text(json.dumps(data), encoding="utf-8")
    result = outcome(resolver, credential)
    assert isinstance(result, auth.AuthenticationUnavailable) and result.reason == "configuration"
    assert result.__cause__ is None and result.__context__ is None
    assert lookup.calls == [A1]  # the stale mapping was not used again


# =============================================================================
# Secrets never appear in logs, exceptions, or the store
# =============================================================================

def test_logs_and_exceptions_contain_no_credential_or_digest(store_path, caplog):
    caplog.set_level(logging.DEBUG, logger="eaiso.auth")
    good, good_entry = auth.issue_credential(store_path, A1)
    inactive, _ = auth.issue_credential(store_path, A2)
    revoked, revoked_entry = auth.issue_credential(store_path, A1)
    auth.revoke_credential(store_path, revoked_entry.credential_id)
    unknown = auth.generate_credential()
    resolver, _ = make_resolver(store_path, {A1: True, A2: False})
    failing, _ = make_resolver(store_path, error=RuntimeError(f"driver said {good}"))

    texts = []
    secrets_used = [good, inactive, revoked, unknown]
    for value in secrets_used + [good + "x"]:
        result = outcome(resolver, value)
        if isinstance(result, auth.AuthenticationError):
            texts += exception_texts(result)
    result = outcome(failing, good)  # even if a dependency error echoed the secret
    texts += exception_texts(result)
    texts.append(caplog.text)
    texts += [repr(r.args) + r.getMessage() for r in caplog.records]
    digests = [auth.credential_digest(v) for v in secrets_used]
    bodies = [v[len(auth.CREDENTIAL_PREFIX):] for v in secrets_used]
    assert_no_secret(texts, *secrets_used, *digests, *bodies)
    assert "auth.verified" in caplog.text and good_entry.credential_id in caplog.text
    assert "reason=revoked" in caplog.text and "reason=unknown" in caplog.text
    assert "reason=inactive_actor" in caplog.text and "reason=database" in caplog.text


def test_store_file_never_contains_raw_credentials(store_path):
    issued = [auth.issue_credential(store_path, A1)[0] for _ in range(3)]
    rotated, _ = auth.rotate_credential(store_path, auth.read_store(store_path)[0].credential_id)
    text = store_path.read_text(encoding="utf-8")
    for value in issued + [rotated]:
        assert value not in text
        assert value[len(auth.CREDENTIAL_PREFIX):] not in text
        assert auth.credential_digest(value) in text


def test_failed_store_write_leaves_previous_store_intact(store_path, monkeypatch):
    credential, entry = auth.issue_credential(store_path, A1)
    before = store_path.read_bytes()

    def broken_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(auth.os, "replace", broken_replace)
    with pytest.raises(OSError):
        auth.issue_credential(store_path, A2)
    with pytest.raises(OSError):
        auth.revoke_credential(store_path, entry.credential_id)
    monkeypatch.undo()
    assert store_path.read_bytes() == before
    assert [p.name for p in store_path.parent.iterdir()] == [store_path.name]  # no temp files
    resolver, _ = make_resolver(store_path)
    assert resolver.resolve(credential).actor_id == A1


def test_writer_refuses_store_the_resolver_would_reject(store_path):
    entry = auth.CredentialEntry("0123456789abcdef", auth.credential_digest(auth.generate_credential()),
                                 A1, auth.ACTIVE, "2026-11-15T00:00:00+00:00")
    with pytest.raises(auth.CredentialConfigurationError):
        auth.write_store(store_path, [entry, entry])
    assert not store_path.exists()


# =============================================================================
# Operator CLI (offline parts)
# =============================================================================

def test_cli_list_rotate_revoke_never_print_digests(store_path, capsys):
    from scripts import manage_test_credentials as cli

    credential, entry = auth.issue_credential(store_path, A1)
    assert cli.main(["--credentials-file", str(store_path), "list"]) == 0
    listed = capsys.readouterr().out
    assert entry.credential_id in listed and "status=active" in listed
    assert_no_secret([listed], credential, entry.digest)

    assert cli.main(["--credentials-file", str(store_path), "rotate",
                     "--credential-id", entry.credential_id]) == 0
    rotated_out = capsys.readouterr().out
    new_credential = rotated_out.strip().splitlines()[-1]
    assert auth.is_well_formed(new_credential)
    assert_no_secret([rotated_out, store_path.read_text(encoding="utf-8")], credential)
    assert new_credential not in store_path.read_text(encoding="utf-8")
    resolver, _ = make_resolver(store_path)
    assert resolver.resolve(new_credential).actor_id == A1

    new_id = auth.read_store(store_path)[-1].credential_id
    assert cli.main(["--credentials-file", str(store_path), "revoke", "--credential-id", new_id]) == 0
    assert isinstance(outcome(resolver, new_credential), auth.Unauthenticated)
    assert cli.main(["--credentials-file", str(store_path), "remove", "--credential-id", "ffffffffffffffff"]) == 1


def test_cli_issue_refuses_non_local_database(store_path):
    from scripts import manage_test_credentials as cli

    with pytest.raises(SystemExit, match="non-local host"):
        cli.main(["--credentials-file", str(store_path), "issue", "--actor-id", str(A1),
                  "--database-url", "postgresql://u:p@db.example.invalid:5432/eaiso_test"])
    assert not store_path.exists()


def test_non_boolean_actor_status_fails_closed(store_path):
    credential, _ = auth.issue_credential(store_path, A1)
    for value in (1, "true", object()):
        resolver, _ = make_resolver(store_path, {A1: value})
        assert isinstance(outcome(resolver, credential), auth.InactiveActor)


def test_secret_echoing_dependency_error_is_unreachable_through_chaining(store_path):
    credential, _ = auth.issue_credential(store_path, A1)
    resolver, _ = make_resolver(store_path, error=RuntimeError(f"driver said {credential}"))
    result = outcome(resolver, credential)
    chain, seen = [], result
    while seen is not None:
        chain.append(seen)
        seen = seen.__cause__ or seen.__context__
    assert chain == [result]
    assert_no_secret([repr(e) for e in chain], credential)


def test_concurrent_writers_do_not_lose_updates(store_path):
    from concurrent.futures import ThreadPoolExecutor

    first, first_entry = auth.issue_credential(store_path, A1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        issued = list(pool.map(lambda _: auth.issue_credential(store_path, A2), range(16)))
        revoke = pool.submit(auth.revoke_credential, store_path, first_entry.credential_id)
        revoke.result(timeout=30)
    entries = {e.credential_id: e for e in auth.read_store(store_path)}
    assert len(entries) == 17
    assert all(entry.credential_id in entries for _, entry in issued)
    assert entries[first_entry.credential_id].status == auth.REVOKED
    resolver, _ = make_resolver(store_path)
    assert isinstance(outcome(resolver, first), auth.Unauthenticated)
    assert all(resolver.resolve(c).actor_id == A2 for c, _ in issued)
    assert not store_path.with_name(store_path.name + ".lock").exists()


def test_held_writer_lock_times_out_without_writing(store_path, monkeypatch):
    auth.issue_credential(store_path, A1)
    before = store_path.read_bytes()
    lock = store_path.with_name(store_path.name + ".lock")
    lock.write_text("")
    monkeypatch.setattr(auth, "LOCK_TIMEOUT_S", 0.2)
    with pytest.raises(auth.CredentialConfigurationError, match="locked by another writer"):
        auth.revoke_credential(store_path, auth.read_store(store_path)[0].credential_id)
    assert store_path.read_bytes() == before
    assert lock.exists()  # a foreign lock is never deleted by a waiting writer


# =============================================================================
# PostgreSQL-backed tests (dedicated eaiso_test only)
# =============================================================================

@pytest.fixture
def auth_dsn():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for eaiso_test; database cases not executed")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    if conninfo_to_dict(dsn).get("dbname") != "eaiso_test":
        pytest.fail("Safety guard: TEST_DATABASE_URL must refer to eaiso_test")
    with psycopg.connect(dsn) as conn:
        name, actor_table = conn.execute(
            "SELECT current_database(), to_regclass('public.actor')").fetchone()
    if name != "eaiso_test":
        pytest.fail("Safety guard: actual connected database is not eaiso_test")
    if actor_table is None:
        pytest.fail("Apply db/003_actor_identity_and_grants.sql to eaiso_test first")
    return dsn


@pytest.fixture
def db_actors(auth_dsn):
    """Isolated synthetic organization and actors, removed afterwards."""
    import psycopg

    from src import identity_provisioning as prov

    conn = prov.connect(auth_dsn)
    org = conn.execute("INSERT INTO organization (name, slug) VALUES (%s, %s) RETURNING id",
                       ("Fictional credential-test organization",
                        f"credential-test-{uuid4().hex}")).fetchone()[0]
    created = []

    def make(kind="provider"):
        actor_id = (prov.create_actor(conn, "synthetic credential-test actor", prov.PROVIDER)
                    if kind == "provider" else
                    prov.create_actor(conn, "synthetic credential-test actor", prov.CUSTOMER, org))
        created.append(actor_id)
        return actor_id

    try:
        yield auth_dsn, conn, org, make
    finally:
        conn.close()
        with psycopg.connect(auth_dsn, autocommit=True) as c:
            c.execute("DELETE FROM organization_membership WHERE actor_id = ANY(%s)", (created,))
            c.execute("DELETE FROM actor WHERE id = ANY(%s)", (created,))
            c.execute("DELETE FROM organization WHERE id = %s", (org,))


def db_resolver(dsn, store_path, **kwargs):
    return auth.CredentialResolver(auth.FileCredentialStore(store_path),
                                   auth.PostgresActorLookup(dsn, **kwargs))


@pytest.mark.db
def test_db_valid_credential_resolves_active_actor(db_actors, store_path):
    dsn, _, _, make = db_actors
    actor = make()
    credential, entry = auth.issue_credential(store_path, actor)
    assert db_resolver(dsn, store_path).resolve(credential) == auth.VerifiedActor(actor, entry.credential_id)


@pytest.mark.db
def test_db_inactive_actor_is_403_and_reactivation_restores(db_actors, store_path):
    dsn, conn, _, make = db_actors
    actor = make("customer")
    credential, _ = auth.issue_credential(store_path, actor)
    resolver = db_resolver(dsn, store_path)
    conn.execute("UPDATE actor SET is_active = FALSE WHERE id = %s", (actor,))
    assert isinstance(outcome(resolver, credential), auth.InactiveActor)
    conn.execute("UPDATE actor SET is_active = TRUE WHERE id = %s", (actor,))
    assert resolver.resolve(credential).actor_id == actor


@pytest.mark.db
def test_db_credential_mapped_to_missing_actor_is_401(db_actors, store_path):
    dsn, _, _, _ = db_actors
    credential, _ = auth.issue_credential(store_path, uuid4())
    result = outcome(db_resolver(dsn, store_path), credential)
    assert isinstance(result, auth.Unauthenticated) and result.reason == "actor_not_found"


@pytest.mark.db
def test_db_authentication_is_not_authorization(db_actors, store_path):
    """An actor with no grants, or only revoked grants, still authenticates."""
    from src import identity_provisioning as prov

    dsn, conn, org, make = db_actors
    no_grants, revoked_grant = make(), make()
    prov.grant_access(conn, revoked_grant, org, prov.SERVICE_ENGINEER)
    prov.deactivate_grant(conn, revoked_grant, org)
    c1, _ = auth.issue_credential(store_path, no_grants)
    c2, _ = auth.issue_credential(store_path, revoked_grant)
    resolver = db_resolver(dsn, store_path)
    assert resolver.resolve(c1).actor_id == no_grants
    assert resolver.resolve(c2).actor_id == revoked_grant


@pytest.mark.db
def test_db_rotation_preserves_actor_against_real_database(db_actors, store_path):
    dsn, _, _, make = db_actors
    actor = make()
    old, entry = auth.issue_credential(store_path, actor)
    new, _ = auth.rotate_credential(store_path, entry.credential_id)
    resolver = db_resolver(dsn, store_path)
    assert resolver.resolve(new).actor_id == actor
    assert isinstance(outcome(resolver, old), auth.Unauthenticated)


@pytest.mark.db
def test_db_unreachable_database_is_unavailable(auth_dsn, store_path, caplog):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    caplog.set_level(logging.DEBUG, logger="eaiso.auth")
    credential, _ = auth.issue_credential(store_path, uuid4())
    params = conninfo_to_dict(auth_dsn)
    marker_password = "unreachable-" + uuid4().hex
    unreachable = make_conninfo(host="127.0.0.1", port="1", dbname="eaiso_test",
                                user=params.get("user", "eaiso_dev"), password=marker_password)
    result = outcome(db_resolver(unreachable, store_path, connect_timeout_s=2), credential)
    assert isinstance(result, auth.AuthenticationUnavailable) and result.reason == "database"
    assert_no_secret([caplog.text, *exception_texts(result)], credential, marker_password)


@pytest.mark.db
def test_db_statement_timeout_is_unavailable(db_actors, store_path):
    """A real server-side failure (statement timeout behind a table lock) fails closed."""
    import psycopg

    dsn, _, _, make = db_actors
    actor = make()
    credential, _ = auth.issue_credential(store_path, actor)
    with psycopg.connect(dsn, autocommit=True) as holder:
        with holder.transaction():
            holder.execute("SET LOCAL lock_timeout = '5s'")
            holder.execute("LOCK TABLE actor IN ACCESS EXCLUSIVE MODE")
            result = outcome(db_resolver(dsn, store_path, statement_timeout_ms=300), credential)
    assert isinstance(result, auth.AuthenticationUnavailable) and result.reason == "database"
    assert db_resolver(dsn, store_path).resolve(credential).actor_id == actor


@pytest.mark.db
def test_db_resolution_writes_nothing(db_actors, store_path):
    dsn, conn, _, make = db_actors
    actor = make()
    credential, _ = auth.issue_credential(store_path, actor)
    before = conn.execute("SELECT xmin::text, is_active FROM actor WHERE id = %s", (actor,)).fetchone()
    for value in (credential, auth.generate_credential(), None):
        outcome(db_resolver(dsn, store_path), value)
    after = conn.execute("SELECT xmin::text, is_active FROM actor WHERE id = %s", (actor,)).fetchone()
    assert before == after


@pytest.mark.db
def test_db_cli_issue_checks_actor_and_issued_credential_resolves(db_actors, store_path, capsys):
    from scripts import manage_test_credentials as cli

    dsn, _, _, make = db_actors
    actor = make()
    assert cli.main(["--credentials-file", str(store_path), "issue", "--actor-id", str(uuid4()),
                     "--database-url", dsn]) == 1
    assert not store_path.exists()
    capsys.readouterr()
    assert cli.main(["--credentials-file", str(store_path), "issue", "--actor-id", str(actor),
                     "--database-url", dsn]) == 0
    credential = capsys.readouterr().out.strip().splitlines()[-1]
    assert credential not in store_path.read_text(encoding="utf-8")
    assert db_resolver(dsn, store_path).resolve(credential).actor_id == UUID(str(actor))


def test_str_subclass_cannot_redirect_the_digest_to_another_credential(store_path):
    """Readiness-review regression: only an exact ``str`` is a credential.

    A ``str`` subclass can pass the format check on its own buffer while
    overriding ``encode`` so the digest is computed from another actor's
    credential. In-process only, but identity must not depend on caller types.
    """
    victim, _ = auth.issue_credential(store_path, A2)

    class Redirecting(str):
        def encode(self, *args, **kwargs):
            return str.encode(victim, *args, **kwargs)

    forged = Redirecting(auth.generate_credential())
    assert auth.is_well_formed(str(forged))
    resolver, _ = make_resolver(store_path)
    result = outcome(resolver, forged)
    assert isinstance(result, auth.Unauthenticated) and result.reason == "malformed"
    assert resolver.resolve(victim).actor_id == A2  # the real credential still works
