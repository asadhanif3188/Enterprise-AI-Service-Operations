# ADR-0005: Server-Verified Identity and Organization-Scoped Case Authorization

- **Status:** Accepted (2026-11-15). The architecture owner approved the **architecture**. Acceptance is not proof of implementation: no credential resolver, identity tables, policy code, HTTP endpoint, or authorization test exists.
- **Recorded:** 2026-11-15
- **Decision provenance:** Contemporary proposal, derived from the [identity model](../identity-model.md), [permission policy](../authorization-permissions.md), and [request sequence](../authentication-authorization-sequence.md).
  - **Finalized at acceptance (2026-11-15):**
    - the inactive-actor status (`403`);
    - the credential lifecycle rules;
    - the minimum revocation contract and lock order;
    - acceptance of requester create-only.
- **Related requirements:** FR-01, FR-02, FR-03, FR-09; rules "Organization ownership" and "Auditability"; quality requirement "Security" ([requirements](../requirements.md))
- **Implementation evidence:** None for authentication or tenant authorization.
  - [`src/case_service.py`](../../src/case_service.py) still filters by a caller-supplied organization ID.
  - Its [tests](../../tests/case-operations-results.md) show that query scoping, **not** authenticated tenant isolation.

## Problem

The local case service accepts a caller-supplied organization UUID and never establishes who the caller is. Exposing it through HTTP would let any caller choose an organization without proving membership or permission.

The platform needs one consistent security boundary for case creation, single-case reads, and organization-scoped listing. It must not present local test credentials as production authentication.

## Constraints

- **Nothing exists yet.** There is no HTTP API, credential resolver, identity or membership table, policy code, or cross-tenant HTTP test. No row-level security is enabled, and a foreign key does not prove membership.
- **Existing decisions:**
  - PostgreSQL is authoritative ([ADR-0001](0001-postgresql-authoritative-case-store.md)).
  - AI output cannot grant permission ([ADR-0002](0002-ai-suggestions-non-authoritative.md)).
  - `transition_case()` stays unexposed ([ADR-0003](0003-application-controlled-case-transitions.md)).
  - [ADR-0004](0004-keyed-idempotent-case-creation.md) (Accepted) scopes idempotency keys to the verified actor and authorized organization defined here.
- **Proposed data model:** a stable `actor.id`, one role per `(actor_id, organization_id)` membership, and only the permissions `case:create`, `case:read`, and `case:list`.
- **Local demonstration only.** The credentials are for local testing. This is not OIDC, SSO, or JWT authentication.

## Options Considered

1. **Server-verified actors, a single authorized-case-operations layer, and organization-scoped SQL (chosen).**
   - *Benefits:* fits a small Python application, and the policy can be tested in one place.
   - *Costs:* the layer must not be bypassed, and it does not stop privileged database writers.
2. **Checks in each HTTP handler or in middleware (rejected).** One handler or future worker can easily forget the check, and middleware lacks the persisted case owner needed for per-case decisions.
3. **PostgreSQL row-level security as the primary enforcement (rejected for now).** It still needs trusted identity and secure per-request database context, and it adds operational complexity before any API exists. It remains a candidate for defense in depth.

## Decision

**Adopt option 1.**

1. **Trusted identity.**
   - **Credentials.** Opaque test credentials, generated with a cryptographically secure random generator (for example, Python's `secrets`). They are verified against server-controlled configuration that clients cannot modify.
   - **Actor resolution.** Verification resolves a stable `actor.id` that is independent of the credential value. Identity never comes from display names, headers, body fields, organization IDs, or unverified token claims.
   - **Handling.** Working credentials are never committed, and credential values are never logged.
   - **Lifecycle.** Revocation removes or disables the credential mapping. Rotation replaces the credential and keeps the same `actor.id`, which also keeps the ADR-0004 idempotency scope.
   - **Failure statuses:**

     | Situation | Status |
     |---|---|
     | Missing, invalid, unknown, or revoked credential | `401` |
     | Valid credential whose `actor.id` has no `actor` row (a configuration error) | `401` |
     | Valid credential for an **inactive** actor | `403` |

   - **Left to implementation:** the configuration mechanism and how credentials are compared, for example hashed lookup or constant-time comparison. Any choice must meet this contract.
2. **Central enforcement.**
   - **Single entry point.** The **authorized-case-operations layer** is the only route-facing entry point to protected case operations. Routes authenticate the caller and call this layer, which calls the permission policy itself.
   - **What the policy checks:** an active actor, an active membership in the evaluated organization, and the action's permission.
   - **What is denied:** unknown actions, missing or disabled memberships, and unknown roles.
   - **No direct access.** Routes never call `src/case_service.py` functions or issue case SQL directly. Forbidden access paths are detected by an automated structural check plus code review.
   - **Limits.** The lower-level functions are not inherently inaccessible. This is application-layer enforcement and does not stop a privileged database client. Row-level security and least-privilege database permissions are defense-in-depth candidates and are **not implemented**.
3. **Organization isolation.**
   - **Create and list:** the client's `organization_id` is an untrusted selector. Membership and permission in that organization are verified independently.
   - **Read by ID:** the case's **persisted** owner is used.
   - **Denial outcomes:**
     - an unauthorized or nonexistent organization on a syntactically valid create or list gets `403`;
     - a missing, foreign, or unreadable case gets the same generic `404` status and body.
   - **No fallback.** A request never falls back to a query across all organizations, and permissions are evaluated separately for each organization.
4. **Minimum role-permission contract.** Every permission requires an active actor and an active membership in the target organization.

   | Role | `case:create` | `case:read` | `case:list` |
   |---|---|---|---|
   | `service_engineer` | Allow | Allow | Allow |
   | `service_manager` | Allow | Allow | Allow |
   | `requester` | Allow, in its own member organization | Deny | Deny |

   - A requester cannot later `GET` even a case it created. Requester status visibility is deferred until case ownership is modeled.
   - Platform administrators receive no implicit access across customers.
   - No update, transition, AI-approval, or membership-management permission is introduced.
   - Unknown roles and actions are denied.
5. **Minimum revocation contract.** Revocation is not instantaneous for requests already in flight.
   - **Reads and lists.**
     - Run one organization-scoped SQL statement that contains predicates for the verified actor and an active membership. It uses the persisted owner for reads and the authorized organization for lists.
     - For reads, a preceding actor-status check distinguishes an inactive actor (`403`); the combined statement repeats the actor predicate.
     - The statement sees its snapshot, so a request that started before a revocation committed may complete under that snapshot.
   - **Writes.**
     - Use a short READ COMMITTED transaction.
     - Lock the actor row `FOR SHARE`, then the membership row `FOR SHARE`, in **two separate statements**. One joined locking statement does not guarantee lock order.
     - Hold both locks through the protected write and the commit.
     - **Ordering:**
       - if a revocation commits first, the locking read waits for it and re-evaluates its `WHERE` clause on the updated row, so it sees the inactive state and denies;
       - if the operation takes its locks first, the revocation waits, and the operation may complete.
     - For keyed creation, the idempotency lookup or reservation, including the read of an existing record on replay, happens inside this locked transaction.
   - **Lock strength.** `FOR KEY SHARE` must not be used instead: it does not block updates to non-key columns such as `is_active` or `role`.
   - **Implementation concerns that need tests:**
     - lock contention;
     - deadlocks (revocations that touch both rows should also lock actor, then membership);
     - bounded waits;
     - transaction failures, which fail closed.
   - **Restrictions.** Locks are never held during model calls, long-running jobs, or client network I/O. Authorization decisions are not cached across requests.
6. **Fail closed.**
   - Authentication or authorization failures never fall back to unscoped access.
   - A database or policy lookup failure prevents the protected operation from starting, and the response is a generic server error.
   - A failure after a write has begun may leave the commit outcome uncertain. A timeout is never reported as a successful authorization or a rollback.
   - Logs never expose credentials, private case content, or another organization's records.

**Compatibility with ADR-0004 (confirmed, with no change to ADR-0004):**

- keys are scoped to `(authorized organization_id, verified actor_id, 'case:create', key)`;
- authorization runs before every key lookup, including replay;
- a revoked actor or membership cannot obtain a replay;
- a replay returns only the immutable `id` and `organization_id`, so a create-only requester never sees current case state;
- credential rotation that keeps the `actor.id` keeps the key scope;
- keyed creation stays disabled until both ADRs are implemented and their tests pass.

Detailed flows and proposed test vectors are in the [request sequence](../authentication-authorization-sequence.md) and the [permission policy](../authorization-permissions.md).

## Risks and Consequences

- **No hard isolation.** In-process enforcement is not cryptographic isolation. Alternate entry points and privileged database connections can bypass it unless restricted independently.
- **Revocation is not instant.** In-flight requests may complete after a revocation commits. Write locks add contention and the risk of deadlocks.
- **Requester visibility.** A requester's later `GET` returns `404`, even for its own case.
- **Status leak.** Returning `403` for an inactive actor reveals that a credential is valid. That is accepted, because only the credential holder receives it.
- **Not production identity.** Test credentials are not production identity. The production identity provider, credential lifecycle, auditing, and database privileges remain separate work.
- **Side channels.** The generic `404` removes a direct signal that a case exists, but not timing side channels.
- **No evidence.** There is no implementation evidence. The case-service tests are not authorization tests.

## Revisit When

**Accepted architectural contracts (change only through a superseding ADR):**

- trusted identity and the credential lifecycle statuses;
- central enforcement through the authorized-case-operations layer;
- the role-permission matrix;
- organization scoping and the denial outcomes;
- the minimum revocation contract and lock order.

**Before enabling HTTP endpoints:**

- Implement and execute positive and negative authentication and authorization tests:
  - missing, unknown, revoked, and rotated credentials;
  - inactive actors;
  - spoofed headers;
  - cross-organization create, read, and list;
  - disabled memberships, mixed roles, and unknown roles;
  - revocation ordering under concurrency;
  - failure paths.
- Assert the database state for each test, and add the structural no-bypass check.
- Record the results separately.

**Before enabling keyed creation:** implement ADR-0004 and pass all of its idempotency, concurrency, cross-actor, header, canonicalization, and authorization tests.

**Before production use:**

- a production identity provider;
- least-privilege database access, and optional row-level security;
- verified audit attribution;
- the full credential lifecycle;
- resource limits and further isolation controls.

Also revisit when background workers, direct database writers, privileged support operations, or service-to-service calls are introduced.
