"""Local application-controlled case operations; no authentication or approval yet.

The organization ID is a query scope, NOT proof that a caller is allowed to
access that organization's data. Do not expose these functions directly as
public endpoints until server-side authentication/authorization is designed.
"""

from __future__ import annotations

from uuid import UUID

# Explicitly limited to the initial forward-only workflow.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "NEW": frozenset({"IN_REVIEW"}),
    "IN_REVIEW": frozenset({"RESOLVED"}),
    "RESOLVED": frozenset({"CLOSED"}),
    "CLOSED": frozenset(),
}


class CaseOperationError(Exception):
    """Base class for controlled case operation failures."""


class CaseNotFound(CaseOperationError):
    """Case is absent or outside the supplied organization scope."""


class VersionConflict(CaseOperationError):
    """The caller's case revision is stale."""


class InvalidTransition(CaseOperationError):
    """The requested case state change is not permitted."""


def _uuid(value: str, name: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name} must be a valid UUID") from exc


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank text")
    return value


def check_transition(source: str, target: str) -> None:
    """Reject unknown, no-op, skipped, backward, or post-closure transitions."""
    if target not in ALLOWED_TRANSITIONS.get(source, frozenset()):
        raise InvalidTransition(f"Transition {source!r} -> {target!r} is not permitted")


def create_case(dsn: str, organization_id: str, title: str, description: str) -> dict:
    """Create a case under an existing organization, initial state NEW/v1."""
    import psycopg
    from psycopg.rows import dict_row

    org_id = _uuid(organization_id, "organization_id")
    _required_text(title, "title")
    _required_text(description, "description")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM organization WHERE id = %s", (org_id,))
            if cur.fetchone() is None:
                raise CaseNotFound("Organization not found")
            cur.execute(
                """INSERT INTO case_record (organization_id, title, description)
                   VALUES (%s, %s, %s)
                   RETURNING id, organization_id, title, description,
                             status, version, created_at, updated_at""",
                (org_id, title, description),
            )
            return dict(cur.fetchone())


def get_case(dsn: str, case_id: str, organization_id: str) -> dict:
    """Retrieve one case, scoped by organization. This is not authorization."""
    import psycopg
    from psycopg.rows import dict_row

    record_id = _uuid(case_id, "case_id")
    org_id = _uuid(organization_id, "organization_id")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, organization_id, title, description,
                          status, version, created_at, updated_at
                   FROM case_record
                   WHERE id = %s AND organization_id = %s""",
                (record_id, org_id),
            )
            record = cur.fetchone()
            if record is None:
                raise CaseNotFound("Case not found in supplied organization scope")
            return dict(record)


def _record_transition(cur, *, case_id, source, target, before_version, actor_label) -> None:
    """Insert history within the same transaction as the status update."""
    cur.execute(
        """INSERT INTO case_status_event
             (case_id, from_status, to_status, from_version, to_version, actor_label)
             VALUES (%s, %s, %s, %s, %s, %s)""",
        (case_id, source, target, before_version, before_version + 1, actor_label),
    )


def transition_case(
    dsn: str,
    case_id: str,
    organization_id: str,
    *,
    expected_version: int,
    target_status: str,
    actor_label: str,
) -> dict:
    """Atomically change status and record an event, or roll back both.

    The actor label is supplied by the local caller; it is not a verified
    identity, approval, or authorization decision.
    """
    import psycopg
    from psycopg.rows import dict_row

    record_id = _uuid(case_id, "case_id")
    org_id = _uuid(organization_id, "organization_id")
    if type(expected_version) is not int or expected_version <= 0:
        raise ValueError("expected_version must be a positive integer")
    _required_text(actor_label, "actor_label")
    if not isinstance(target_status, str):
        raise InvalidTransition("target_status must be a string")

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status, version FROM case_record
                   WHERE id = %s AND organization_id = %s FOR UPDATE""",
                (record_id, org_id),
            )
            current = cur.fetchone()
            if current is None:
                raise CaseNotFound("Case not found in supplied organization scope")
            if current["version"] != expected_version:
                raise VersionConflict(
                    f"Expected case version {expected_version}, found {current['version']}"
                )
            check_transition(current["status"], target_status)
            cur.execute(
                """UPDATE case_record
                   SET status = %s, version = version + 1,
                       updated_at = clock_timestamp()
                   WHERE id = %s AND organization_id = %s AND version = %s
                   RETURNING id, organization_id, title, description,
                             status, version, created_at, updated_at""",
                (target_status, record_id, org_id, expected_version),
            )
            updated = cur.fetchone()
            if updated is None:
                raise VersionConflict("The case changed before its update completed")
            _record_transition(
                cur, case_id=record_id, source=current["status"],
                target=target_status, before_version=expected_version,
                actor_label=actor_label,
            )
            return dict(updated)
