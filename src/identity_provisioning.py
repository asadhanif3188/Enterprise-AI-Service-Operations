"""Trusted local provisioning of synthetic actors and customer access grants.

Scope (ADR-0006, Accepted):

* For a trusted local operator or development/test setup only. This is NOT an
  authenticated or authorized self-service path, and no HTTP endpoint calls it.
  Whoever can run it with a database connection can create any eligible grant.
* Every mutation runs in its own short READ COMMITTED transaction (or, when
  grouped explicitly with ``provisioning_transaction()``, a savepoint of one)
  that locks the actor row first (``FOR NO KEY UPDATE``) and only then reads or writes that
  actor's grants. Multi-grant changes touch grants in ``organization_id``
  order. No lock is held across model calls or network operations.
* Eligibility is validated here before any write, and again by the PostgreSQL
  guards in ``db/003_actor_identity_and_grants.sql``. Either layer rejects an
  ineligible grant; the database guard is the backstop for other writers.
* A deadlock, lock timeout, statement timeout, or other database error is
  raised as ``ProvisioningError``. It is never reported as a successful grant.

Affiliation is not authorization: nothing here grants case access on its own.
Request-time authorization (ADR-0005) is not implemented by this module.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

PROVIDER = "provider"
CUSTOMER = "customer"
AFFILIATION_TYPES = frozenset({PROVIDER, CUSTOMER})

SERVICE_ENGINEER = "service_engineer"
SERVICE_MANAGER = "service_manager"
REQUESTER = "requester"
MEMBERSHIP_ROLES = frozenset({SERVICE_ENGINEER, SERVICE_MANAGER, REQUESTER})

DEFAULT_LOCK_TIMEOUT_MS = 5000
DEFAULT_STATEMENT_TIMEOUT_MS = 15000

# Constraint names raised by the database guards (SQLSTATE 23514).
_ELIGIBILITY_CONSTRAINTS = frozenset({
    "organization_membership_role_eligible",
    "actor_grants_remain_eligible",
})


class ProvisioningError(Exception):
    """The requested change was not applied."""


class EligibilityError(ProvisioningError):
    """The grant or affiliation combination is denied by ADR-0006."""


class ProvisioningConflict(ProvisioningError):
    """The requested change conflicts with existing provisioned state."""


class NotFound(ProvisioningError):
    """The referenced actor, organization, or grant does not exist."""


@dataclass(frozen=True)
class Grant:
    organization_id: UUID
    role: str
    is_active: bool


# ----- Pure eligibility policy (mirrors role_grant_is_eligible in SQL) --------

def is_eligible(affiliation_type: str, home_organization_id: UUID | None,
                organization_id: UUID, role: str) -> bool:
    """Return True only for the combinations ADR-0006 allows. All else is denied."""
    if affiliation_type == PROVIDER:
        return role in (SERVICE_ENGINEER, SERVICE_MANAGER)
    if affiliation_type == CUSTOMER:
        return (role == REQUESTER and home_organization_id is not None
                and organization_id == home_organization_id)
    return False


def check_eligibility(affiliation_type: str, home_organization_id: UUID | None,
                      organization_id: UUID, role: str) -> None:
    if not is_eligible(affiliation_type, home_organization_id, organization_id, role):
        raise EligibilityError(
            f"{affiliation_type} actor is not eligible for a {role} grant "
            f"in organization {organization_id}"
        )


def check_affiliation_shape(affiliation_type: str, home_organization_id: UUID | None) -> None:
    """Mirror of the actor_affiliation_consistent CHECK constraint."""
    if affiliation_type not in AFFILIATION_TYPES:
        raise ValueError("affiliation_type must be 'provider' or 'customer'")
    if affiliation_type == PROVIDER and home_organization_id is not None:
        raise ValueError("a provider actor must not have a home organization")
    if affiliation_type == CUSTOMER and home_organization_id is None:
        raise ValueError("a customer actor requires a home organization")


def _uuid(value, name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name} must be a valid UUID") from exc


def _optional_uuid(value, name: str) -> UUID | None:
    return None if value is None else _uuid(value, name)


def _role(value: str) -> str:
    if value not in MEMBERSHIP_ROLES:
        raise ValueError(f"role must be one of {sorted(MEMBERSHIP_ROLES)}")
    return value


# ----- Connection and transaction helpers ------------------------------------

def connect(dsn: str):
    """Open an autocommit connection; each operation manages its own transaction."""
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


# Connections currently inside provisioning_transaction(). Operations called on
# such a connection run as savepoints within it; any other open transaction is
# refused, so a successful return always means "committed" unless the caller
# explicitly grouped work with provisioning_transaction().
_OPEN_PROVISIONING_TX: "weakref.WeakSet" = weakref.WeakSet()


class _Transaction:
    """READ COMMITTED provisioning transaction with bounded waits and error mapping."""

    def __init__(self, conn, lock_timeout_ms: int, statement_timeout_ms: int,
                 *, group: bool = False):
        self.conn = conn
        self.lock_timeout_ms = lock_timeout_ms
        self.statement_timeout_ms = statement_timeout_ms
        self.group = group
        self._tx = None

    def __enter__(self):
        import psycopg
        from psycopg.pq import TransactionStatus

        nested = self.conn in _OPEN_PROVISIONING_TX
        if nested and self.group:
            raise ProvisioningError("provisioning_transaction() cannot be nested")
        if not nested and self.conn.info.transaction_status != TransactionStatus.IDLE:
            raise ProvisioningError(
                "Provisioning refuses to run inside a caller-managed transaction, "
                "because its success would not mean committed; use provisioning_transaction()"
            )
        try:
            self._tx = self.conn.transaction()
            self._tx.__enter__()
        except psycopg.Error as exc:
            raise _translate(exc) from exc
        if not nested:
            try:
                self.conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                # SET LOCAL equivalents: they end with this transaction.
                self.conn.execute(
                    "SELECT set_config('lock_timeout', %s, true),"
                    "       set_config('statement_timeout', %s, true)",
                    (f"{int(self.lock_timeout_ms)}ms", f"{int(self.statement_timeout_ms)}ms"),
                )
            except BaseException as exc:
                self.__exit__(type(exc), exc, exc.__traceback__)
                raise
        if self.group:
            _OPEN_PROVISIONING_TX.add(self.conn)
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        import psycopg

        if self.group:
            _OPEN_PROVISIONING_TX.discard(self.conn)
        try:
            self._tx.__exit__(exc_type, exc, tb)
        except psycopg.Error as commit_exc:
            raise _translate(commit_exc) from commit_exc
        if exc is not None and isinstance(exc, psycopg.Error):
            raise _translate(exc) from exc
        return False


def provisioning_transaction(conn, *, lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
                             statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS):
    """Group several provisioning operations into one READ COMMITTED transaction.

    Operations called inside the block run as savepoints. Their return values
    are provisional: nothing is applied unless the block exits normally and
    its COMMIT succeeds (a failed COMMIT raises ProvisioningError). An
    operation that raises inside the block has been rolled back to its
    savepoint. The block holds every lock it took until it ends, so keep it
    short and never make model or network calls inside it. Not nestable.
    """
    return _Transaction(conn, lock_timeout_ms, statement_timeout_ms, group=True)


def _translate(exc) -> ProvisioningError:
    from psycopg import errors

    diag = getattr(exc, "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    message = getattr(diag, "message_primary", None) or str(exc)
    if isinstance(exc, errors.CheckViolation) and constraint in _ELIGIBILITY_CONSTRAINTS:
        return EligibilityError(f"Database guard {constraint}: {message}")
    if isinstance(exc, errors.UniqueViolation):
        return ProvisioningConflict("A grant for this actor and organization already exists")
    if isinstance(exc, errors.ForeignKeyViolation):
        return NotFound("Referenced actor or organization does not exist")
    if isinstance(exc, errors.DeadlockDetected):
        return ProvisioningError("Deadlock detected; the change was rolled back and not applied")
    if isinstance(exc, errors.LockNotAvailable):
        return ProvisioningError("Lock timeout; the change was rolled back and not applied")
    if isinstance(exc, errors.QueryCanceled):
        return ProvisioningError("Statement timeout; the change was rolled back and not applied")
    if isinstance(exc, errors.SerializationFailure):
        return ProvisioningError("Serialization failure; the change was rolled back and not applied")
    return ProvisioningError(f"Database error ({type(exc).__name__}); the change was not applied")


def _transaction(conn, lock_timeout_ms, statement_timeout_ms) -> _Transaction:
    return _Transaction(conn, lock_timeout_ms, statement_timeout_ms)


def lock_actor(conn, actor_id) -> tuple[str, UUID | None]:
    """Lock the actor row first (FOR NO KEY UPDATE) and return its affiliation.

    Must be called inside a transaction. FOR NO KEY UPDATE conflicts with the
    membership guard's FOR SHARE and with other provisioning transactions, so
    all provisioning for one actor is serialized and acquires the actor lock
    before any of that actor's grant rows.
    """
    from psycopg.pq import TransactionStatus

    if conn.info.transaction_status == TransactionStatus.IDLE:
        raise ProvisioningError("lock_actor must be called inside a transaction")
    row = conn.execute(
        """SELECT affiliation_type, home_organization_id
             FROM actor WHERE id = %s FOR NO KEY UPDATE""",
        (_uuid(actor_id, "actor_id"),),
    ).fetchone()
    if row is None:
        raise NotFound("Actor not found")
    return row[0], row[1]


def _require_organization(conn, organization_id: UUID) -> None:
    if conn.execute("SELECT 1 FROM organization WHERE id = %s",
                    (organization_id,)).fetchone() is None:
        raise NotFound("Organization not found")


def _grant_row(conn, actor_id: UUID, organization_id: UUID, *, lock: bool):
    sql = ("SELECT role, is_active FROM organization_membership "
           "WHERE actor_id = %s AND organization_id = %s")
    if lock:
        sql += " FOR UPDATE"
    return conn.execute(sql, (actor_id, organization_id)).fetchone()


# ----- Provisioning operations -----------------------------------------------

def create_actor(conn, display_name: str, affiliation_type: str,
                 home_organization_id=None, *,
                 lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
                 statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> UUID:
    """Create an actor with no grants. The database generates its id."""
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("display_name must be nonblank text")
    home = _optional_uuid(home_organization_id, "home_organization_id")
    check_affiliation_shape(affiliation_type, home)
    with _transaction(conn, lock_timeout_ms, statement_timeout_ms):
        if home is not None:
            _require_organization(conn, home)
        return conn.execute(
            """INSERT INTO actor (display_name, affiliation_type, home_organization_id)
               VALUES (%s, %s, %s) RETURNING id""",
            (display_name, affiliation_type, home),
        ).fetchone()[0]


def grant_access(conn, actor_id, organization_id, role: str, *,
                 lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
                 statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> None:
    """Create one explicitly approved, active grant. Never reactivates or overwrites."""
    actor = _uuid(actor_id, "actor_id")
    org = _uuid(organization_id, "organization_id")
    _role(role)
    with _transaction(conn, lock_timeout_ms, statement_timeout_ms):
        affiliation, home = lock_actor(conn, actor)
        check_eligibility(affiliation, home, org, role)
        _require_organization(conn, org)
        if _grant_row(conn, actor, org, lock=False) is not None:
            raise ProvisioningConflict(
                "A grant already exists for this actor and organization; "
                "use change_grant_role or reactivate_grant explicitly"
            )
        conn.execute(
            """INSERT INTO organization_membership (actor_id, organization_id, role)
               VALUES (%s, %s, %s)""",
            (actor, org, role),
        )


def change_grant_role(conn, actor_id, organization_id, role: str, *,
                      lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
                      statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> None:
    """Change the role of an existing grant in place; its active state is preserved."""
    actor = _uuid(actor_id, "actor_id")
    org = _uuid(organization_id, "organization_id")
    _role(role)
    with _transaction(conn, lock_timeout_ms, statement_timeout_ms):
        affiliation, home = lock_actor(conn, actor)
        if _grant_row(conn, actor, org, lock=True) is None:
            raise NotFound("Grant not found")
        check_eligibility(affiliation, home, org, role)
        conn.execute(
            """UPDATE organization_membership SET role = %s
               WHERE actor_id = %s AND organization_id = %s""",
            (role, actor, org),
        )


def deactivate_grant(conn, actor_id, organization_id, *,
                     lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
                     statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> bool:
    """Revoke a grant by deactivating it. Returns False if it was already inactive."""
    actor = _uuid(actor_id, "actor_id")
    org = _uuid(organization_id, "organization_id")
    with _transaction(conn, lock_timeout_ms, statement_timeout_ms):
        lock_actor(conn, actor)
        row = _grant_row(conn, actor, org, lock=True)
        if row is None:
            raise NotFound("Grant not found")
        if not row[1]:
            return False
        conn.execute(
            """UPDATE organization_membership SET is_active = FALSE
               WHERE actor_id = %s AND organization_id = %s""",
            (actor, org),
        )
        return True


def reactivate_grant(conn, actor_id, organization_id, *,
                     lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
                     statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> bool:
    """Reactivate a historical grant after revalidating eligibility.

    Returns False if it was already active.
    """
    actor = _uuid(actor_id, "actor_id")
    org = _uuid(organization_id, "organization_id")
    with _transaction(conn, lock_timeout_ms, statement_timeout_ms):
        affiliation, home = lock_actor(conn, actor)
        row = _grant_row(conn, actor, org, lock=True)
        if row is None:
            raise NotFound("Grant not found")
        if row[1]:
            return False
        check_eligibility(affiliation, home, org, row[0])
        conn.execute(
            """UPDATE organization_membership SET is_active = TRUE
               WHERE actor_id = %s AND organization_id = %s""",
            (actor, org),
        )
        return True


def change_affiliation(conn, actor_id, affiliation_type: str, home_organization_id=None, *,
                       deactivate_incompatible: bool = False,
                       lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
                       statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> list[UUID]:
    """Change an actor's affiliation.

    By default the change is rejected while any active grant would become
    ineligible. With ``deactivate_incompatible=True`` the operator explicitly
    asks for those grants to be deactivated (never deleted) in the same
    transaction, in ``organization_id`` order. Returns the organization IDs of
    grants that were deactivated.
    """
    actor = _uuid(actor_id, "actor_id")
    home = _optional_uuid(home_organization_id, "home_organization_id")
    check_affiliation_shape(affiliation_type, home)
    with _transaction(conn, lock_timeout_ms, statement_timeout_ms):
        lock_actor(conn, actor)
        if home is not None:
            _require_organization(conn, home)
        active = conn.execute(
            """SELECT organization_id, role FROM organization_membership
               WHERE actor_id = %s AND is_active
               ORDER BY organization_id FOR UPDATE""",
            (actor,),
        ).fetchall()
        incompatible = [org for org, role in active
                        if not is_eligible(affiliation_type, home, org, role)]
        if incompatible and not deactivate_incompatible:
            raise EligibilityError(
                f"Affiliation change rejected: {len(incompatible)} active grant(s) "
                "would become ineligible; deactivate or reconcile them first"
            )
        for org in incompatible:
            conn.execute(
                """UPDATE organization_membership SET is_active = FALSE
                   WHERE actor_id = %s AND organization_id = %s""",
                (actor, org),
            )
        conn.execute(
            """UPDATE actor SET affiliation_type = %s, home_organization_id = %s
               WHERE id = %s""",
            (affiliation_type, home, actor),
        )
        return incompatible


def list_grants(conn, actor_id) -> list[Grant]:
    rows = conn.execute(
        """SELECT organization_id, role, is_active FROM organization_membership
           WHERE actor_id = %s ORDER BY organization_id""",
        (_uuid(actor_id, "actor_id"),),
    ).fetchall()
    return [Grant(*row) for row in rows]


def eligibility_violations(conn) -> list[tuple]:
    """Active grants that violate ADR-0006, e.g. after a restore. Expected: []."""
    return conn.execute(
        """SELECT actor_id, organization_id, role, affiliation_type, home_organization_id
             FROM organization_membership_eligibility_violation
            ORDER BY actor_id, organization_id"""
    ).fetchall()


# ----- Synthetic development scenario ----------------------------------------

@dataclass(frozen=True)
class SyntheticActor:
    key: str
    display_name: str
    affiliation_type: str
    home: str | None            # "A" or "B" for customer actors
    grants: tuple[tuple[str, str], ...]  # (organization "A"/"B", role)


# All names are fictional. Organization A and B map to existing synthetic
# development organizations (northwind and summit by default). The provider
# (ABC) has no organization row.
SYNTHETIC_ACTORS: tuple[SyntheticActor, ...] = (
    SyntheticActor("ahmed", "Ahmed (synthetic requester, Organization A)",
                   CUSTOMER, "A", (("A", REQUESTER),)),
    SyntheticActor("fatima", "Fatima (synthetic requester, Organization B)",
                   CUSTOMER, "B", (("B", REQUESTER),)),
    SyntheticActor("ali", "Ali (synthetic ABC service engineer)",
                   PROVIDER, None, (("A", SERVICE_ENGINEER), ("B", SERVICE_ENGINEER))),
    SyntheticActor("sara", "Sara (synthetic ABC service manager)",
                   PROVIDER, None, (("A", SERVICE_MANAGER), ("B", SERVICE_MANAGER))),
    SyntheticActor("bilal", "Bilal (synthetic ABC platform administrator, no customer grants)",
                   PROVIDER, None, ()),
)


@dataclass(frozen=True)
class ScenarioResult:
    actor_ids: dict[str, UUID]
    created_actors: tuple[str, ...]
    created_grants: tuple[tuple[str, str, str], ...]


def _org_id_by_slug(conn, slug: str) -> UUID:
    row = conn.execute("SELECT id FROM organization WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        raise NotFound(f"Organization with slug {slug!r} not found; load db/seed_dev.sql first")
    return row[0]


def _iter_scenario(org_ids: dict[str, UUID], label_suffix: str) -> Iterator[tuple]:
    for spec in SYNTHETIC_ACTORS:
        home = org_ids[spec.home] if spec.home else None
        yield spec, spec.display_name + label_suffix, home


def provision_synthetic_scenario(conn, *, org_a_slug: str = "northwind",
                                 org_b_slug: str = "summit",
                                 label_suffix: str = "") -> ScenarioResult:
    """Converge the fictional Ahmed/Fatima/Ali/Sara/Bilal scenario. Safe to rerun.

    * Missing actors and grants are created through the normal provisioning
      operations (actor-first locks, eligibility checks).
    * Existing matching actors (by exact synthetic display label) and identical
      active grants are left unchanged.
    * Anything that differs, such as a changed affiliation, a different role, an
      inactive grant, or two actors with the same synthetic label, raises
      ProvisioningConflict. It is never silently overwritten or reactivated.
    * Each actor and grant is committed by its own short transaction. If a run
      stops part-way, the steps already completed remain; a rerun continues.
    * Concurrent runs are refused through a session-level advisory lock, so two
      runs cannot both create an actor with the same synthetic label.
    """
    if not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))",
                        (_SCENARIO_LOCK_KEY,)).fetchone()[0]:
        raise ProvisioningConflict("Another synthetic scenario provisioning run is in progress")
    try:
        return _provision_scenario(conn, org_a_slug, org_b_slug, label_suffix)
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_SCENARIO_LOCK_KEY,))


_SCENARIO_LOCK_KEY = "eaiso.synthetic_identity_scenario"


def _provision_scenario(conn, org_a_slug: str, org_b_slug: str,
                        label_suffix: str) -> ScenarioResult:
    org_ids = {"A": _org_id_by_slug(conn, org_a_slug), "B": _org_id_by_slug(conn, org_b_slug)}
    actor_ids: dict[str, UUID] = {}
    created_actors: list[str] = []
    created_grants: list[tuple[str, str, str]] = []

    for spec, label, home in _iter_scenario(org_ids, label_suffix):
        rows = conn.execute(
            "SELECT id, affiliation_type, home_organization_id FROM actor WHERE display_name = %s",
            (label,),
        ).fetchall()
        if len(rows) > 1:
            raise ProvisioningConflict(f"Several actors are labelled {label!r}; resolve manually")
        if rows:
            actor_id, affiliation, existing_home = rows[0]
            if (affiliation, existing_home) != (spec.affiliation_type, home):
                raise ProvisioningConflict(
                    f"Existing actor {label!r} has a different affiliation; not changed"
                )
        else:
            actor_id = create_actor(conn, label, spec.affiliation_type, home)
            created_actors.append(spec.key)
        actor_ids[spec.key] = actor_id

        existing = {g.organization_id: g for g in list_grants(conn, actor_id)}
        for org_key, role in spec.grants:
            org = org_ids[org_key]
            grant = existing.get(org)
            if grant is None:
                grant_access(conn, actor_id, org, role)
                created_grants.append((spec.key, org_key, role))
            elif (grant.role, grant.is_active) != (role, True):
                raise ProvisioningConflict(
                    f"Existing grant for {label!r} in organization {org_key} is "
                    f"({grant.role}, active={grant.is_active}); expected ({role}, active=True). "
                    "Not changed; use the explicit grant operations."
                )
        unexpected = set(existing) - {org_ids[o] for o, _ in spec.grants}
        if unexpected:
            raise ProvisioningConflict(
                f"Actor {label!r} has grants outside the synthetic scenario; not changed"
            )

    return ScenarioResult(actor_ids, tuple(created_actors), tuple(created_grants))
