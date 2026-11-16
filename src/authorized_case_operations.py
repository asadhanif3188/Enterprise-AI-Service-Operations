"""Authorized case operations: the single route-facing entry point (ADR-0005).

Future HTTP handlers must call only this layer for protected case operations.
Each entry point:

1. takes the caller's **opaque credential**, never an actor ID, and resolves it
   with the credential resolver (identity only);
2. evaluates the permission policy itself, inside the same database work as
   the protected operation; callers cannot pass in a decision or a scope;
3. restricts SQL to the authorized organization.

Create is a short READ COMMITTED transaction that locks the actor row, then
the membership row, ``FOR SHARE`` in two statements and holds both locks
through the insert and the commit. Read and list each run one scoped SQL
statement (read is preceded by an actor-status check that distinguishes an
inactive actor). A read that started before a revocation committed may
complete under its snapshot: revocation is not instantaneous for in-flight
requests.

Not provided here: HTTP, keyed idempotent creation (ADR-0004), list cursors
(deferred to the HTTP increment), status transitions, and AI approval.
``src/case_service.py`` is unchanged and remains local, unauthenticated code;
it is not called from this layer.

Outcomes are typed exceptions (``src/case_authorization.py`` and
``src/credential_auth.py``); none carries credentials, case text, or SQL.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from src import case_authorization as policy
from src.case_authorization import (
    CaseCreationOutcomeUnknown,
    CaseNotFound,
    CaseOperationUnavailable,
    IdempotencyPayloadMismatch,
    IdempotencyRequestInFlight,
    InvalidCaseRequest,
    OrganizationAccessDenied,
)
from src.credential_auth import (
    InactiveActor,
    Unauthenticated,
    VerifiedActor,
    resolver_from_environment,
)
from src.request_fingerprint import FINGERPRINT_VERSION, case_create_fingerprint

logger = logging.getLogger("eaiso.authz")

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
# Input bound chosen by this layer (the schema has none); revisit with the API contract.
MAX_TEXT_LENGTH = 10_000
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

CASE_FIELDS = ("id", "organization_id", "title", "description", "status", "version",
               "created_at", "updated_at")
LIST_FIELDS = ("id", "organization_id", "title", "status", "version", "created_at")
REPLAY_FIELDS = ("id", "organization_id")

# ADR-0004: the decoded Idempotency-Key value (case-sensitive).
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._~-]{16,128}")
DEFAULT_IDEMPOTENCY_WAIT_MS = 2000


class Resolver(Protocol):
    def resolve(self, credential: object) -> VerifiedActor: ...


def _uuid(value: object, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    # Only the canonical hyphenated form (any case); braces, "urn:uuid:", and
    # unhyphenated forms are rejected so one identifier has one spelling.
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise InvalidCaseRequest(f"{name}_malformed")
    return UUID(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCaseRequest(f"{name}_blank")
    # Checked before any lock is taken: PostgreSQL text cannot hold NUL, and a
    # bounded size keeps the locked create transaction short.
    if "\x00" in value:
        raise InvalidCaseRequest(f"{name}_invalid_character")
    if len(value) > MAX_TEXT_LENGTH:
        raise InvalidCaseRequest(f"{name}_too_long")
    try:
        value.encode("utf-8")  # e.g. a lone surrogate, which the driver cannot send
    except UnicodeEncodeError:
        unencodable = True
    else:
        unencodable = False
    if unencodable:  # raised outside the except block: no chained text-bearing error
        raise InvalidCaseRequest(f"{name}_invalid_character")
    return value


def _idempotency_key(value: object) -> str:
    if type(value) is not str or _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        raise InvalidCaseRequest("idempotency_key_malformed")
    return value


@dataclass(frozen=True)
class KeyedCaseCreation:
    """Outcome of a keyed create: a new case, or a replay of the caller's own earlier one.

    ``representation`` is the full creation representation for a new case, and
    only ``id`` and ``organization_id`` (read from the idempotency record, never
    from the case) for a replay.
    """

    replayed: bool
    representation: dict


def _limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIST_LIMIT:
        raise InvalidCaseRequest("limit_out_of_range")
    return value


def _reason(exc: BaseException) -> str:
    from psycopg import errors

    if isinstance(exc, errors.LockNotAvailable):
        return "lock_timeout"
    if isinstance(exc, errors.QueryCanceled):
        return "statement_timeout"
    return "database"


class AuthorizedCaseOperations:
    """Protected case create, read, and list for a verified caller."""

    def __init__(self, resolver: Resolver, dsn: str, *, connect_timeout_s: int = 5,
                 lock_timeout_ms: int = 5000, statement_timeout_ms: int = 10000,
                 idempotency_wait_ms: int = DEFAULT_IDEMPOTENCY_WAIT_MS):
        if not 1 <= int(idempotency_wait_ms) < int(statement_timeout_ms):
            raise ValueError("idempotency_wait_ms must be positive and below the statement timeout")
        self._resolver = resolver
        self._dsn = dsn
        self._connect_timeout_s = connect_timeout_s
        self._lock_timeout_ms = lock_timeout_ms
        self._statement_timeout_ms = statement_timeout_ms
        self._idempotency_wait_ms = int(idempotency_wait_ms)

    # ----- infrastructure ----------------------------------------------------------

    def _connect(self, *, autocommit: bool):
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(
            self._dsn, autocommit=autocommit, row_factory=dict_row,
            connect_timeout=self._connect_timeout_s,
            options=(f"-c lock_timeout={int(self._lock_timeout_ms)} "
                     f"-c statement_timeout={int(self._statement_timeout_ms)}"),
        )
        if not autocommit:
            conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        return conn

    def _verify(self, credential: object) -> UUID:
        # Identity comes only from the resolver; its outcomes (401/403/500
        # equivalents) propagate unchanged.
        return self._resolver.resolve(credential).actor_id

    @staticmethod
    def _deny(action: str, actor_id: UUID, error: Exception) -> Exception:
        logger.info("authz.denied action=%s actor_id=%s reason=%s",
                    action, actor_id, getattr(error, "reason", type(error).__name__))
        return error

    @staticmethod
    def _authorize_create(conn, actor_id: UUID, org_id: UUID) -> tuple[Exception | None, dict | None]:
        """ADR-0005 write authorization inside the caller's transaction.

        Statement 1 locks the actor row FOR SHARE. Under READ COMMITTED a
        concurrent, uncommitted deactivation makes it wait; afterwards the latest
        committed row version is returned. Statement 2 then locks the membership
        row FOR SHARE, separately, so the lock order is always actor, then
        membership. Both locks are held until the transaction ends. Returns
        ``(denial, None)`` or ``(None, grant)``.
        """
        actor = conn.execute(
            "SELECT is_active FROM actor WHERE id = %s FOR SHARE", (actor_id,)
        ).fetchone()
        if actor is None:
            return Unauthenticated("actor_not_found"), None
        if actor["is_active"] is not True:
            return InactiveActor(), None
        grant = conn.execute(
            """SELECT organization_id, role, is_active FROM organization_membership
               WHERE actor_id = %s AND organization_id = %s FOR SHARE""",
            (actor_id, org_id),
        ).fetchone()
        if (grant is None or grant["is_active"] is not True
                or not policy.permits(grant["role"], policy.CASE_CREATE)):
            return OrganizationAccessDenied(), None
        return None, grant

    # ----- create -------------------------------------------------------------------

    def create_case(self, credential: object, organization_id: object,
                    title: object, description: object) -> dict:
        """Create a case in the selected organization if the caller may.

        ``organization_id`` is an untrusted selector. Not idempotent: separate
        successful calls create separate cases.
        """
        actor_id = self._verify(credential)
        org_id = _uuid(organization_id, "organization_id")
        title_text = _text(title, "title")
        description_text = _text(description, "description")

        import psycopg

        phase = "connect"
        failure: tuple[str, str] | None = None
        denial: Exception | None = None
        created: dict | None = None
        try:
            with self._connect(autocommit=False) as conn:
                phase = "authorize"
                denial, grant = self._authorize_create(conn, actor_id, org_id)
                if denial is None:
                    assert grant is not None
                    phase = "write"
                    created = conn.execute(
                        """INSERT INTO case_record (organization_id, title, description)
                           VALUES (%s, %s, %s)
                           RETURNING id, organization_id, title, description,
                                     status, version, created_at, updated_at""",
                        (grant["organization_id"], title_text, description_text),
                    ).fetchone()
                if denial is not None:
                    conn.rollback()
                else:
                    phase = "commit"
                    conn.commit()  # both row locks are held until here
        except psycopg.Error as exc:
            failure = (phase, _reason(exc))
        except Exception:  # noqa: BLE001 - any other error still fails closed
            # Not chained or echoed: a non-driver error could carry case text.
            failure = (phase, "internal")

        if failure is not None:
            phase, reason = failure
            logger.error("authz.unavailable action=%s actor_id=%s phase=%s reason=%s",
                         policy.CASE_CREATE, actor_id, phase, reason)
            if phase == "commit":
                raise CaseCreationOutcomeUnknown()
            raise CaseOperationUnavailable(reason)
        if denial is not None:
            raise self._deny(policy.CASE_CREATE, actor_id, denial)
        assert created is not None
        logger.info("authz.allowed action=%s actor_id=%s organization_id=%s case_id=%s",
                    policy.CASE_CREATE, actor_id, created["organization_id"], created["id"])
        return {field: created[field] for field in CASE_FIELDS}

    def create_case_keyed(self, credential: object, organization_id: object, title: object,
                          description: object, idempotency_key: object) -> KeyedCaseCreation:
        """Keyed, idempotent create (ADR-0004), scoped to the authorized organization
        and the verified actor.

        Algorithm, in one short READ COMMITTED transaction:

        1. Authorize exactly as for an unkeyed create (actor, then membership,
           ``FOR SHARE``; ``case:create``). Nothing about keys is read before this.
        2. **Reserve** the scope: insert the idempotency record first, with a
           freshly generated case ID, ``ON CONFLICT DO NOTHING``. The primary key
           arbitrates. If a concurrent transaction holds an uncommitted record for
           the same scope, this statement waits for it, bounded by
           ``idempotency_wait_ms``; on expiry nothing was written and the result
           is ``IdempotencyRequestInFlight`` (``503``).
        3. If the reservation won, insert the case with that ID and commit. The
           record's composite foreign key to ``(organization_id, case_id)`` is
           checked at commit, so both rows commit together or not at all.
        4. If it lost (a committed record exists), read that record in the same
           scope and roll back. No speculative case was ever inserted, so a
           losing attempt cannot leave one behind. An equal fingerprint is a
           replay (``id`` and ``organization_id`` from the record only); any
           other fingerprint is ``IdempotencyPayloadMismatch``.

        A failure during COMMIT is ``CaseCreationOutcomeUnknown``; retrying with
        the same key then resolves it (a replay if the case committed, a create
        if it did not).
        """
        actor_id = self._verify(credential)
        org_id = _uuid(organization_id, "organization_id")
        title_text = _text(title, "title")
        description_text = _text(description, "description")
        key = _idempotency_key(idempotency_key)
        fingerprint = case_create_fingerprint(org_id, title_text, description_text)

        import psycopg

        phase = "connect"
        failure: tuple[str, str] | None = None
        in_flight = False
        denial: Exception | None = None
        created: dict | None = None
        existing: dict | None = None
        try:
            with self._connect(autocommit=False) as conn:
                phase = "authorize"
                denial, grant = self._authorize_create(conn, actor_id, org_id)
                if denial is None:
                    assert grant is not None
                    authorized_org = grant["organization_id"]
                    new_case_id = uuid4()
                    conn.execute("SELECT set_config('lock_timeout', %s, true)",
                                 (f"{self._idempotency_wait_ms}ms",))
                    # Only this statement's lock waits map to 503 (it also takes
                    # the foreign-key locks on the organization and actor rows,
                    # which do not conflict with ordinary activity).
                    phase = "reserve"
                    reserved = conn.execute(
                        """INSERT INTO case_creation_idempotency
                               (organization_id, actor_id, operation, idempotency_key,
                                fingerprint_version, request_fingerprint, case_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (organization_id, actor_id, operation, idempotency_key)
                           DO NOTHING
                           RETURNING case_id""",
                        (authorized_org, actor_id, policy.CASE_CREATE, key,
                         FINGERPRINT_VERSION, fingerprint, new_case_id),
                    ).fetchone()
                    phase = "reserved"
                    conn.execute("SELECT set_config('lock_timeout', %s, true)",
                                 (f"{int(self._lock_timeout_ms)}ms",))
                    if reserved is None:
                        phase = "lookup"
                        existing = conn.execute(
                            """SELECT organization_id, case_id, fingerprint_version,
                                      request_fingerprint
                                 FROM case_creation_idempotency
                                WHERE organization_id = %s AND actor_id = %s
                                  AND operation = %s AND idempotency_key = %s""",
                            (authorized_org, actor_id, policy.CASE_CREATE, key),
                        ).fetchone()
                        if existing is None:
                            raise RuntimeError("conflicting record not visible")
                    else:
                        phase = "write"
                        created = conn.execute(
                            """INSERT INTO case_record (id, organization_id, title, description)
                               VALUES (%s, %s, %s, %s)
                               RETURNING id, organization_id, title, description,
                                         status, version, created_at, updated_at""",
                            (new_case_id, authorized_org, title_text, description_text),
                        ).fetchone()
                if denial is not None or existing is not None:
                    conn.rollback()
                else:
                    phase = "commit"
                    conn.commit()  # case and record together; both row locks held until here
        except psycopg.Error as exc:
            failure = (phase, _reason(exc))
            in_flight = phase == "reserve" and isinstance(exc, psycopg.errors.LockNotAvailable)
        except Exception:  # noqa: BLE001 - any other error still fails closed
            failure = (phase, "internal")

        if failure is not None:
            phase, reason = failure
            if in_flight:
                logger.warning("authz.idempotency_in_flight action=%s actor_id=%s",
                               policy.CASE_CREATE, actor_id)
                raise IdempotencyRequestInFlight()
            logger.error("authz.unavailable action=%s actor_id=%s phase=%s reason=%s keyed=true",
                         policy.CASE_CREATE, actor_id, phase, reason)
            if phase == "commit":
                raise CaseCreationOutcomeUnknown()
            raise CaseOperationUnavailable(reason)
        if denial is not None:
            raise self._deny(policy.CASE_CREATE, actor_id, denial)
        if existing is not None:
            if (existing["fingerprint_version"] == FINGERPRINT_VERSION
                    and existing["request_fingerprint"] == fingerprint):
                logger.info("authz.allowed action=%s actor_id=%s organization_id=%s case_id=%s "
                            "idempotency=replay", policy.CASE_CREATE, actor_id,
                            existing["organization_id"], existing["case_id"])
                return KeyedCaseCreation(True, {"id": existing["case_id"],
                                                "organization_id": existing["organization_id"]})
            logger.info("authz.denied action=%s actor_id=%s reason=idempotency_payload_mismatch",
                        policy.CASE_CREATE, actor_id)
            raise IdempotencyPayloadMismatch()
        assert created is not None
        logger.info("authz.allowed action=%s actor_id=%s organization_id=%s case_id=%s "
                    "idempotency=created", policy.CASE_CREATE, actor_id,
                    created["organization_id"], created["id"])
        return KeyedCaseCreation(False, {field: created[field] for field in CASE_FIELDS})

    # ----- read ---------------------------------------------------------------------

    def get_case(self, credential: object, case_id: object) -> dict:
        """Return one case if the caller may read it in the case's persisted organization.

        Missing, foreign, and unreadable cases raise the same CaseNotFound.
        """
        actor_id = self._verify(credential)
        case_uuid = _uuid(case_id, "case_id")

        import psycopg

        failure: str | None = None
        actor = row = None
        try:
            with self._connect(autocommit=True) as conn:
                # Preceding actor-status check (ADR-0005): distinguishes 403 for an
                # inactive actor. The scoped statement repeats the actor predicate.
                actor = conn.execute(
                    "SELECT is_active FROM actor WHERE id = %s", (actor_id,)).fetchone()
                if actor is not None and actor["is_active"] is True:
                    # One statement: ownership from the persisted case, plus active
                    # actor, active membership in that organization, and a role that
                    # grants case:read.
                    row = conn.execute(
                        """SELECT c.id, c.organization_id, c.title, c.description,
                                  c.status, c.version, c.created_at, c.updated_at
                             FROM case_record AS c
                             JOIN organization_membership AS m
                               ON m.organization_id = c.organization_id
                              AND m.actor_id = %(actor_id)s
                              AND m.is_active
                              AND m.role = ANY(%(roles)s)
                             JOIN actor AS a
                               ON a.id = m.actor_id
                              AND a.is_active
                            WHERE c.id = %(case_id)s""",
                        {"actor_id": actor_id, "case_id": case_uuid,
                         "roles": policy.roles_permitting(policy.CASE_READ)},
                    ).fetchone()
        except psycopg.Error as exc:
            failure = _reason(exc)
        except Exception:  # noqa: BLE001 - fail closed without chaining
            failure = "internal"

        if failure is not None:
            logger.error("authz.unavailable action=%s actor_id=%s reason=%s",
                         policy.CASE_READ, actor_id, failure)
            raise CaseOperationUnavailable(failure)
        if actor is None:
            raise self._deny(policy.CASE_READ, actor_id, Unauthenticated("actor_not_found"))
        if actor["is_active"] is not True:
            raise self._deny(policy.CASE_READ, actor_id, InactiveActor())
        if row is None:
            raise self._deny(policy.CASE_READ, actor_id, CaseNotFound())
        logger.info("authz.allowed action=%s actor_id=%s case_id=%s",
                    policy.CASE_READ, actor_id, row["id"])
        return {field: row[field] for field in CASE_FIELDS}

    # ----- list ---------------------------------------------------------------------

    def list_cases(self, credential: object, organization_id: object, *,
                   limit: int = DEFAULT_LIST_LIMIT) -> dict:
        """List cases of exactly one selected organization, if the caller may.

        Returns ``{"items": [...], "has_more": bool}`` ordered by
        ``(created_at, id)``. Cursors are deferred to the HTTP increment; there
        is no way to request another organization's rows or all rows.
        """
        actor_id = self._verify(credential)
        org_id = _uuid(organization_id, "organization_id")
        page_size = _limit(limit)

        import psycopg

        failure: str | None = None
        rows: list[dict] = []
        try:
            with self._connect(autocommit=True) as conn:
                # One statement, one snapshot: actor status, the authorization
                # decision, and the case rows, which are produced only when the
                # decision is true and only for the selected organization.
                rows = conn.execute(
                    """SELECT s.actor_active, g.authorized,
                              c.id, c.organization_id, c.title, c.status, c.version, c.created_at
                         FROM (SELECT (SELECT is_active FROM actor WHERE id = %(actor_id)s)
                                      AS actor_active) AS s
                        CROSS JOIN LATERAL (
                              SELECT EXISTS (
                                  SELECT 1
                                    FROM actor AS a
                                    JOIN organization_membership AS m ON m.actor_id = a.id
                                   WHERE a.id = %(actor_id)s
                                     AND a.is_active
                                     AND m.organization_id = %(org_id)s
                                     AND m.is_active
                                     AND m.role = ANY(%(roles)s)
                              ) AS authorized) AS g
                         LEFT JOIN LATERAL (
                              SELECT c.id, c.organization_id, c.title, c.status,
                                     c.version, c.created_at
                                FROM case_record AS c
                               WHERE g.authorized
                                 AND c.organization_id = %(org_id)s
                               ORDER BY c.created_at, c.id
                               LIMIT %(fetch)s) AS c ON TRUE
                        ORDER BY c.created_at, c.id""",
                    {"actor_id": actor_id, "org_id": org_id, "fetch": page_size + 1,
                     "roles": policy.roles_permitting(policy.CASE_LIST)},
                ).fetchall()
        except psycopg.Error as exc:
            failure = _reason(exc)
        except Exception:  # noqa: BLE001 - fail closed without chaining
            failure = "internal"

        if failure is not None:
            logger.error("authz.unavailable action=%s actor_id=%s reason=%s",
                         policy.CASE_LIST, actor_id, failure)
            raise CaseOperationUnavailable(failure)
        head = rows[0]
        if head["actor_active"] is None:
            raise self._deny(policy.CASE_LIST, actor_id, Unauthenticated("actor_not_found"))
        if head["actor_active"] is not True:
            raise self._deny(policy.CASE_LIST, actor_id, InactiveActor())
        if head["authorized"] is not True:
            raise self._deny(policy.CASE_LIST, actor_id, OrganizationAccessDenied())
        items = [{field: r[field] for field in LIST_FIELDS} for r in rows if r["id"] is not None]
        logger.info("authz.allowed action=%s actor_id=%s organization_id=%s count=%d",
                    policy.CASE_LIST, actor_id, org_id, min(len(items), page_size))
        return {"items": items[:page_size], "has_more": len(items) > page_size}


DATABASE_URL_ENV = "DATABASE_URL"
IDEMPOTENCY_WAIT_ENV = "EAISO_IDEMPOTENCY_WAIT_MS"


class OperationsConfigurationError(Exception):
    """Mandatory server configuration is missing or invalid. Messages never echo values."""


def _database_url_from_environment() -> str:
    import os

    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get(DATABASE_URL_ENV, "")
    if not dsn.strip():
        raise OperationsConfigurationError(f"{DATABASE_URL_ENV} is not set")
    try:
        params = conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - the parser's message could echo the value
        params = None
    if not params or not params.get("dbname"):
        raise OperationsConfigurationError(
            f"{DATABASE_URL_ENV} is not a valid PostgreSQL connection string with a database name")
    return dsn


def _idempotency_wait_from_environment() -> int:
    import os

    raw = os.environ.get(IDEMPOTENCY_WAIT_ENV, "").strip()
    if not raw:
        return DEFAULT_IDEMPOTENCY_WAIT_MS
    if not raw.isdigit() or not 1 <= int(raw) <= 9000:
        raise OperationsConfigurationError(
            f"{IDEMPOTENCY_WAIT_ENV} must be an integer number of milliseconds from 1 to 9000")
    return int(raw)


def operations_from_environment(dsn: str | None = None,
                                store_path: Path | None = None) -> AuthorizedCaseOperations:
    """Build the layer with the real credential resolver from server-side configuration.

    This is the only construction path route modules may import (the structural
    check forbids routes from importing the class or a resolver), so a route
    cannot supply its own resolver and thereby choose the caller's identity.

    Called without arguments (as the HTTP application does), the database URL
    comes from the server's ``DATABASE_URL`` environment variable and the
    credential store from ``EAISO_CREDENTIALS_FILE`` (default
    ``.local/test-credentials.json``); the keyed-create bounded wait from
    ``EAISO_IDEMPOTENCY_WAIT_MS`` (default 2000). Missing or invalid configuration raises
    ``OperationsConfigurationError`` or ``CredentialConfigurationError``, so
    startup fails instead of serving requests.
    """
    if dsn is None:
        dsn = _database_url_from_environment()
    wait_ms = _idempotency_wait_from_environment()
    return AuthorizedCaseOperations(resolver_from_environment(dsn, store_path), dsn,
                                    idempotency_wait_ms=wait_ms)
