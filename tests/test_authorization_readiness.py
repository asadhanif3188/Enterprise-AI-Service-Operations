"""Security-readiness evidence for the integrated identity and authorization path.

These PostgreSQL-backed tests add evidence that the centralized-authorization
suite (tests/test_case_authorization.py) does not already provide:

* credential revocation or rotation in the local file store while a create is
  already in flight (the store is not locked with the database transaction);
* a real network failure at COMMIT, through a TCP proxy that cuts the
  connection either before or after the COMMIT reaches the server, showing why
  the outcome must be reported as unknown;
* a real PostgreSQL deadlock involving the create transaction, with either
  participant chosen as the victim.

They reuse the fictional world, fixtures, and safety guards (``eaiso_test``
only) of the centralized-authorization suite. Each test asserts persisted state.
"""

# Fixtures are imported from the centralized-authorization suite and used as
# test parameters, which ruff reports as unused imports and redefinitions.
# ruff: noqa: F401, F811

from __future__ import annotations

import select
import socket
import threading
import time

import pytest
from test_case_authorization import (
    WAIT_SECONDS,
    authz_dsn,
    outcome,
    pool,
    result_of,
    wait_blocked_by,
    world,
)

from src import credential_auth as auth
from src.authorized_case_operations import AuthorizedCaseOperations
from src.case_authorization import CaseCreationOutcomeUnknown, CaseOperationUnavailable

# =============================================================================
# Credential revocation while a create is in flight
# =============================================================================

@pytest.mark.db
@pytest.mark.parametrize("change", ["revoke", "rotate"])
def test_credential_change_during_in_flight_create_does_not_stop_it(world, pool, change):
    """Observed contract: credentials are verified once, before the transaction.

    The create is held at its INSERT (after resolution and after both FOR SHARE
    locks) while the credential is revoked or rotated in the file store. The
    store is not part of the database transaction, so the in-flight create
    commits. Only later resolutions see the change. Actor deactivation, by
    contrast, is serialized with the create by the actor row lock (covered in
    the centralized-authorization suite).
    """
    org, cred = world.org_a, world.creds["engineer_a"]
    actor = world.actors["engineer_a"]
    entry = next(e for e in auth.read_store(world.store) if e.actor_id == actor)
    gate = world.conn()
    count_before = world.snapshot()[1]
    with gate.transaction():
        gate.execute("LOCK TABLE case_record IN SHARE MODE")
        create = pool.submit(world.ops.create_case, cred, str(org),
                             "Synthetic in-flight case", "Fictional")
        wait_blocked_by(world.admin, gate.info.backend_pid, "INSERT INTO case_record", create)
        if change == "revoke":
            auth.revoke_credential(world.store, entry.credential_id)
            new_cred = None
        else:
            new_cred, _ = auth.rotate_credential(world.store, entry.credential_id)
        # The store change is complete and visible to new resolutions ...
        assert isinstance(outcome(world.resolver.resolve, cred), auth.Unauthenticated)
        assert not create.done()  # ... but the in-flight create is unaffected.
    created = result_of(create)
    assert isinstance(created, dict), created
    row = world.admin.execute("SELECT organization_id FROM case_record WHERE id = %s",
                              (created["id"],)).fetchone()
    assert row == (org,) and world.snapshot()[1] == count_before + 1

    # Every later use of the old credential is denied and writes nothing.
    for call in (lambda: world.ops.create_case(cred, str(org), "x", "y"),
                 lambda: world.ops.get_case(cred, str(created["id"])),
                 lambda: world.ops.list_cases(cred, str(org))):
        result = outcome(call)
        assert isinstance(result, auth.Unauthenticated) and result.reason == "revoked"
    assert world.snapshot()[1] == count_before + 1
    if new_cred is not None:  # rotation keeps the same actor and access
        assert world.ops.get_case(new_cred, str(created["id"]))["id"] == created["id"]


# =============================================================================
# Real network failure at COMMIT
# =============================================================================

COMMIT_MESSAGE = b"Q\x00\x00\x00\x0bCOMMIT\x00"  # simple-query COMMIT, as psycopg sends it
READY_FOR_QUERY = b"Z\x00\x00\x00\x05"


class CommitCuttingProxy:
    """TCP proxy that relays one PostgreSQL connection and cuts it at COMMIT.

    ``before_server``: the COMMIT is never forwarded; the server sees the client
    disappear mid-transaction and rolls back.
    ``after_server``: the COMMIT is forwarded and completes on the server; the
    reply is withheld and the client connection is closed.
    In both modes the client observes a connection failure during COMMIT.
    """

    def __init__(self, target_host: str, target_port: int, mode: str):
        self.target = (target_host, target_port)
        self.mode = mode
        self.commit_seen = threading.Event()
        self.server_committed = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.threads: list[threading.Thread] = []
        self.server_side_ports: list[int] = []
        self._closed = False
        accept = threading.Thread(target=self._accept, daemon=True)
        accept.start()
        self.threads.append(accept)

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            t = threading.Thread(target=self._relay, args=(client,), daemon=True)
            t.start()
            self.threads.append(t)

    def _relay(self, client: socket.socket) -> None:
        server = socket.create_connection(self.target)
        # The server sees this port as the session's client_port.
        self.server_side_ports.append(server.getsockname()[1])
        withheld = b""
        try:
            while True:
                readable, _, _ = select.select([client, server], [], [], 30)
                if not readable:
                    return
                if client in readable:
                    data = client.recv(65536)
                    if not data:
                        return
                    if COMMIT_MESSAGE in data and not self.commit_seen.is_set():
                        self.commit_seen.set()
                        if self.mode == "before_server":
                            return  # cut: COMMIT never reaches the server
                    server.sendall(data)
                if server in readable:
                    data = server.recv(65536)
                    if not data:
                        return
                    if self.commit_seen.is_set() and self.mode == "after_server":
                        withheld += data
                        if READY_FOR_QUERY in withheld:
                            if b"COMMIT" in withheld:
                                self.server_committed.set()
                            return  # cut: the server's reply is withheld
                        continue
                    client.sendall(data)
        finally:
            for s in (client, server):
                try:
                    s.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._closed = True
        self.listener.close()


@pytest.mark.db
@pytest.mark.parametrize("mode,persisted", [("before_server", False), ("after_server", True)])
def test_real_connection_loss_at_commit_is_unknown_outcome(world, mode, persisted):
    """A genuine socket failure during COMMIT, not a monkeypatched exception.

    The layer reports CaseCreationOutcomeUnknown in both cases, and the
    database shows that both outcomes really occur, which is why neither
    success nor rollback may be claimed.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(world.dsn)
    proxy = CommitCuttingProxy(params.get("host") or "127.0.0.1", int(params.get("port") or 5432), mode)
    try:
        proxied = make_conninfo(world.dsn, host="127.0.0.1", port=str(proxy.port),
                                sslmode="disable", gssencmode="disable")
        # Identity resolution uses the direct database; only the case transaction
        # runs through the proxy.
        ops = AuthorizedCaseOperations(world.resolver, proxied)
        title = f"Synthetic commit-cut case {mode}"
        before = world.snapshot()
        result = outcome(ops.create_case, world.creds["engineer_a"], str(world.org_a), title, "Fictional")
    finally:
        proxy.close()
    assert proxy.commit_seen.is_set()
    assert isinstance(result, CaseCreationOutcomeUnknown), result
    assert result.status_equivalent == 500 and result.__context__ is None

    # Wait until the proxied server session has actually ended (rolled back or
    # committed), identified by its client port, before reading state.
    assert proxy.server_side_ports
    deadline = time.monotonic() + WAIT_SECONDS
    while world.admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE client_port = ANY(%s)",
            (proxy.server_side_ports,)).fetchone()[0]:
        assert time.monotonic() < deadline, "proxied session did not end"
        time.sleep(0.05)
    rows = world.admin.execute("SELECT organization_id FROM case_record WHERE title = %s",
                               (title,)).fetchall()
    if persisted:
        assert proxy.server_committed.is_set()
        assert rows == [(world.org_a,)] and world.snapshot()[1] == before[1] + 1
    else:
        assert rows == [] and world.snapshot() == before


# =============================================================================
# Real deadlock involving the create transaction
# =============================================================================

@pytest.mark.db
@pytest.mark.parametrize("victim", ["create", "other_session"])
def test_real_deadlock_fails_closed_or_create_completes(world, pool, victim):
    """Fault injection: another session takes membership, then actor (the
    reverse of the accepted actor-then-membership order, which the
    provisioning code follows). PostgreSQL aborts one participant.

    * Create is the victim: CaseOperationUnavailable (never a denial), no case.
    * The other session is the victim: the create completes normally.
    The victim is chosen with that session's deadlock_timeout (superuser-only).
    """
    actor, org = world.actors["engineer_a"], world.org_a
    admin_role = world.admin.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
    if not admin_role:
        pytest.skip("deadlock_timeout can be set per session only by a superuser")
    assert world.admin.execute("SHOW deadlock_timeout").fetchone()[0] == "1s"
    other = world.conn()
    before = world.snapshot()
    other.execute("BEGIN")
    try:
        other.execute("SET LOCAL deadlock_timeout = %s" % ("'10s'" if victim == "create" else "'100ms'"))
        other.execute("SELECT 1 FROM organization_membership WHERE actor_id = %s "
                      "AND organization_id = %s FOR UPDATE", (actor, org))
        create = pool.submit(world.ops.create_case, world.creds["engineer_a"], str(org),
                             "Synthetic deadlock case", "Fictional")
        create_pid = wait_blocked_by(world.admin, other.info.backend_pid,
                                     "FROM organization_membership", create)
        started = time.monotonic()
        closing = pool.submit(outcome, other.execute,
                              "UPDATE actor SET is_active = is_active WHERE id = %s", (actor,))
        create_result = result_of(create)
        other_result = result_of(closing)
        elapsed = time.monotonic() - started
    finally:
        other.execute("ROLLBACK")
    import psycopg

    if victim == "create":
        assert isinstance(create_result, CaseOperationUnavailable), create_result
        assert create_result.reason == "database"
        assert not isinstance(other_result, Exception), other_result
        assert world.snapshot() == before
    else:
        assert isinstance(other_result, psycopg.errors.DeadlockDetected), other_result
        assert isinstance(create_result, dict), create_result
        assert world.snapshot()[1] == before[1] + 1
    assert create_pid and elapsed < 8
