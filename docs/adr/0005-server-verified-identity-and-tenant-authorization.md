# ADR-0005: Server-Verified Identity and Organization-Scoped Case Authorization

- **Status:** Proposed
- **Recorded:** 2026-11-15
- **Decision provenance:** Contemporary proposal. It is derived from the existing proposed design documents: [identity model](../identity-model.md), [permission policy](../authorization-permissions.md), and [request sequence](../authentication-authorization-sequence.md).
- **Related requirements:** FR-01, FR-02, FR-03, FR-09; rules "Organization ownership" and "Auditability"; quality requirement "Security" ([requirements](../requirements.md))
- **Implementation evidence:** None for authentication or tenant authorization.
  - No `actor` or `organization_membership` migration, credential resolver, HTTP API, or policy code exists.
  - The [case-service tests](../../tests/case-operations-results.md) show filtering by a caller-supplied organization ID, **not** authenticated tenant isolation.

## Problem

The local case service ([`src/case_service.py`](../../src/case_service.py)) accepts a caller-supplied organization UUID and never establishes who the caller is. Exposing it through HTTP would let any caller choose an organization without proving membership or permission.

The platform needs one consistent security boundary for case creation, single-case reads, and organization-scoped listing. It must not present local test credentials as production authentication.

## Constraints

- **Nothing exists yet.** The repository has no HTTP API, credential resolver, identity or membership tables, policy code, or cross-tenant HTTP test. No row-level security is enabled, and a foreign key does not prove membership.
- **Existing decisions:**
  - PostgreSQL is authoritative ([ADR-0001](0001-postgresql-authoritative-case-store.md)).
  - AI output cannot grant permission ([ADR-0002](0002-ai-suggestions-non-authoritative.md)).
  - `transition_case()` stays unexposed ([ADR-0003](0003-application-controlled-case-transitions.md)).
- **Proposed model:** a stable `actor.id`, one role per `(actor_id, organization_id)` membership, and only the permissions `case:create`, `case:read`, and `case:list`.
- **IDs are not credentials.** Client-supplied case and organization UUIDs are selectors, never credentials. Local demonstration credentials must not be published, and must not be mistaken for OIDC or SSO.
- **Idempotency dependency.** [ADR-0004](0004-keyed-idempotent-case-creation.md) is Accepted as design, but not implemented. Its key scope depends on the verified actor and authorized organization that this ADR defines.

## Options Considered

1. **Server-verified actors, with authorization enforced in a single authorized-case-operations layer and organization-scoped SQL (preferred).**
   - *Benefits:* fits a small Python application, and the policy can be tested in one place.
   - *Costs:* routes and future entry points must be prevented from bypassing the layer. It does not stop privileged database writers.
2. **Checks in each HTTP handler, or in middleware only.**
   - *Benefits:* minimal new abstraction.
   - *Costs:* one handler or future worker can easily forget the check. Middleware lacks the persisted case owner needed for decisions about individual cases.
3. **PostgreSQL row-level security as the primary enforcement.**
   - *Benefits:* limits exposure from accidentally unscoped SQL.
   - *Costs:* still needs trusted identity establishment, secure per-request database context, and careful handling of privileged roles and pooled connections. That is a lot of operational complexity before any authenticated API exists. It remains a candidate for defense in depth.

## Decision

**Proposed: adopt option 1**, subject to review by the architecture owner.

- **Authenticate.** The HTTP boundary verifies a local, opaque, test-only bearer credential against server-controlled configuration and resolves an `actor.id`. An invalid credential gets `401`. Headers, display names, client-supplied IDs, model output, and unverified token claims never supply identity. Credentials are never committed or logged.
- **Authorize.** The **authorized-case-operations layer** checks three things, not individual routes:
  - the actor exists and is active;
  - an active membership exists in the organization being evaluated;
  - that membership's role grants the action.

  Unknown roles and actions are denied. Routes never call `src/case_service.py` or case SQL directly. That rule needs a future structural test plus code review.
- **Scope.** For create and list, the requested `organization_id` only selects which membership is checked. For a single-case read, the case's **persisted** owner is used. Every allowed operation is restricted to the authorized organization.
- **Denial outcomes:**
  - a nonexistent or unauthorized organization on create or list gets `403`;
  - a nonexistent, foreign, or unreadable single case gets the same generic `404` status and body;
  - an inactive actor is always denied, and its status code is not settled here.
- **Minimum roles:**
  - `service_engineer` and `service_manager` may create, read, and list in their active member organizations;
  - `requester` is proposed to be **create-only**, with no later read even of its own case until requester ownership is modeled;
  - platform administrators get no implicit access to customer data;
  - no update, transition, AI-approval, or membership-administration permission is granted.
- **The check is tied to the operation.** The policy call is coupled to the database operation, not run as a separate check that could be bypassed.
  - **Candidates, not accepted:** for reads and lists, one scoped statement with membership predicates; for writes, a short transaction that locks the actor and membership rows `FOR SHARE`.
  - **No revocation guarantee or lock order is accepted.**
  - Decisions are not cached across requests in the first version.
- **Fail closed.** If the credential cannot be verified, the policy lookup fails, or the database is unavailable, no protected operation starts. Once a write has begun, a failure can leave the commit outcome unknown, and no automatic rollback is claimed.
- **Idempotency coupling.** The layer authorizes the verified actor **before** any idempotency-key lookup, and it supplies the `(authorized organization_id, verified actor_id)` pair that scopes the keys accepted in ADR-0004. A replay returns only immutable identity, never current case state.

  Keyed creation stays **disabled** until both ADRs are Accepted, their contracts are aligned, and isolation and concurrency tests have been executed.

Detailed behavior and the proposed test vectors are in the [request sequence](../authentication-authorization-sequence.md) and the [permission policy](../authorization-permissions.md).

## Risks and Consequences

- **No hard isolation.** In-process boundaries are not cryptographic isolation. Python code, alternate entry points, and privileged database connections can bypass them unless they are restricted and checked independently.
- **Revocation races.** Revocations and role changes race with in-flight requests. A consistency policy, a lock order, and error behavior must be chosen before any enforcement guarantee is claimed.
- **Requester visibility.** Under create-only, a requester's later `GET` returns `404`, even for the case it just submitted.
- **Not production identity.** Test credentials are not production identity. Credential storage, rotation, and revocation, identity-provider mapping, auditing, and a least-privilege database role all need separate decisions.
- **Side channels.** The generic `404` removes a straightforward existence signal but does not rule out timing side channels.
- **No evidence.** There is no implementation evidence. The case-service tests are not authorization tests.

## Revisit When

- **Before Accepted.** These are design prerequisites, not test executions:
  - settle the inactive-actor status code;
  - settle how test credentials are verified and revoked;
  - define an enforceable rule that the authorized-operations layer cannot be bypassed;
  - define a minimum revocation-timing contract;
  - decide the requester create-only policy;
  - confirm compatibility with ADR-0004's actor-scoped keys and identity-only replay.
- **Before the API is exposed.** Implement and execute tenant-authorization tests, positive and negative. They must cover spoofed headers, wrong-organization create, read, and list, disabled memberships, mixed roles, and failure paths. Add the structural no-bypass check, and record the results separately.
- **Before keyed creation.** ADR-0004 is already accepted as design. Implement it, and execute its cross-actor, revocation, concurrency, header-parsing, canonicalization, and isolation tests.
- **When new data paths appear.** Revisit when background workers, direct database writers, privileged support operations, or service-to-service calls are introduced, and when optional row-level security or a least-privilege database role is considered.
