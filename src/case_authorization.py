"""Case permission policy and authorization outcomes (ADR-0005).

This module is pure: it holds the role-permission matrix and the typed
outcomes, and touches no database. The authorized-case-operations layer
(`src/authorized_case_operations.py`) is the only caller that turns these
rules into SQL predicates and decisions.

`status_equivalent` is the status ADR-0005 assigns to an outcome. No HTTP
layer exists; it is documentation for a future boundary, not a response.

Affiliation (ADR-0006) is deliberately absent: role eligibility is enforced
when grants are written, and adds no request-time predicate here.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

CASE_CREATE = "case:create"
CASE_READ = "case:read"
CASE_LIST = "case:list"
ACTIONS = frozenset({CASE_CREATE, CASE_READ, CASE_LIST})

# The only roles with case permissions. Any other role, including
# platform_administrator (which is not a membership role), has none. Read-only,
# so in-process code cannot widen the matrix at runtime.
ROLE_PERMISSIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    "service_engineer": frozenset({CASE_CREATE, CASE_READ, CASE_LIST}),
    "service_manager": frozenset({CASE_CREATE, CASE_READ, CASE_LIST}),
    "requester": frozenset({CASE_CREATE}),
})


# ----- Outcomes -----------------------------------------------------------------

class CaseAccessError(Exception):
    """Base class for authorization-layer outcomes. Messages never carry case data."""

    status_equivalent = 500

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InvalidCaseRequest(CaseAccessError):
    """400 equivalent: malformed identifier, blank text, or out-of-range limit."""

    status_equivalent = 400


class UnsupportedAction(CaseAccessError):
    """Deny by default: the action is not one of the three case permissions."""

    status_equivalent = 403

    def __init__(self) -> None:
        super().__init__("unsupported_action")


class OrganizationAccessDenied(CaseAccessError):
    """403 equivalent for create and list: no active, permitting membership in the
    selected organization. A nonexistent organization is indistinguishable."""

    status_equivalent = 403

    def __init__(self) -> None:
        super().__init__("organization_access_denied")


class CaseNotFound(CaseAccessError):
    """404 equivalent for read: missing, foreign, and unreadable cases are identical."""

    status_equivalent = 404

    def __init__(self) -> None:
        super().__init__("case_not_found")


class CaseOperationUnavailable(CaseAccessError):
    """Generic dependency failure before any write committed. Never a denial or success."""

    status_equivalent = 500


class CaseCreationOutcomeUnknown(CaseAccessError):
    """The failure happened during COMMIT: the case may or may not exist.

    ADR-0005: a failure after a write has begun is never reported as success
    or as a rollback. A client must treat the outcome as unknown.
    """

    status_equivalent = 500

    def __init__(self) -> None:
        super().__init__("commit_outcome_unknown")


class IdempotencyPayloadMismatch(CaseAccessError):
    """ADR-0004: the caller's own key was already used in this organization with a
    different request fingerprint. Nothing was created or changed, and nothing
    about the earlier request is disclosed."""

    status_equivalent = 422

    def __init__(self) -> None:
        super().__init__("idempotency_payload_mismatch")


class IdempotencyRequestInFlight(CaseAccessError):
    """ADR-0004: a concurrent request with the same scope was still unresolved when
    the bounded wait expired. This request created nothing; the competing
    request's outcome is unknown. Retry with the same key."""

    status_equivalent = 503

    def __init__(self) -> None:
        super().__init__("idempotency_request_in_flight")


# ----- Policy -------------------------------------------------------------------

def require_supported_action(action: object) -> str:
    """Return the action if it is one of the three case permissions; deny otherwise."""
    if not isinstance(action, str) or action not in ACTIONS:
        raise UnsupportedAction()
    return action


def permits(role: object, action: object) -> bool:
    """True only for a known action granted to a known role. Everything else is denied.

    Callers choose the denial outcome for a known action the role lacks:
    403 for create and list, the generic 404 for read.
    """
    if not isinstance(action, str) or action not in ACTIONS or not isinstance(role, str):
        return False
    return action in ROLE_PERMISSIONS.get(role, frozenset())


def roles_permitting(action: str) -> list[str]:
    """Roles that grant ``action``, sorted, for use as a SQL ``role = ANY(...)`` predicate."""
    require_supported_action(action)
    return sorted(role for role, actions in ROLE_PERMISSIONS.items() if action in actions)
