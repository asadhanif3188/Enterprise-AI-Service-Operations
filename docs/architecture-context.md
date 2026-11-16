# Architecture Context

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** Living architecture overview and entry point to the Architecture Decision Records

This document summarizes the architectural boundaries between the application and its AI component, the current implementation, and its known limits. The reasoning behind each decision is recorded in the Architecture Decision Records (ADRs). The [system context](system-context.md) shows the current components, capability status, and trust boundaries.

## Architecture Decision Index

Decisions are recorded as numbered ADRs. The [ADR index](adr/README.md) explains the process and lists every record.

| ADR | Decision | Status | Legacy reference |
|---|---|---|---|
| [ADR-0001](adr/0001-postgresql-authoritative-case-store.md) | PostgreSQL as the Authoritative Case Store | Accepted | DEC-001 |
| [ADR-0002](adr/0002-ai-suggestions-non-authoritative.md) | AI Suggestions Are Non-Authoritative | Accepted | DEC-002 |
| [ADR-0003](adr/0003-application-controlled-case-transitions.md) | Application-Controlled Case State Transitions | Accepted | DEC-003 |
| [ADR-0004](adr/0004-keyed-idempotent-case-creation.md) | Keyed Idempotent Case Creation | Accepted; implemented (keyed creation in the local HTTP API) | [Case creation semantics](case-creation-semantics.md) §3; [results](../tests/keyed-case-creation-results.md) |
| [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) | Server-Verified Identity and Organization-Scoped Case Authorization | Accepted; implemented in Python and a localhost-only HTTP API | [HTTP case API results](../tests/http-case-api-results.md) |
| [ADR-0006](adr/0006-actor-affiliation-and-role-grant-eligibility.md) | Actor Affiliation and Role-Grant Eligibility | Accepted; schema, guards, and local provisioning implemented | — |

**Accepted does not mean implemented.** An Accepted ADR records an adopted direction. Several of these decisions include requirements, such as authorization and approval checks, that the current code does not meet. See [Current boundary](#current-boundary).

## Original decision notes

The project's first architecture decisions were recorded here on 2026-11-11 as three brief notes, DEC-001 to DEC-003. Each note had a decision statement and a one-line rationale. They were not full ADRs: they did not record alternatives, consequences, or revisit conditions. ADR-0001 to ADR-0003 formalize them as retrospective reconstructions. The identifiers remain valid references, and the original wording is preserved in this file's Git history.

### DEC-001 — The application owns service-case data and workflow decisions

Formalized in [ADR-0001](adr/0001-postgresql-authoritative-case-store.md).

- **Authoritative state:** the application owns authoritative case data and state in PostgreSQL, and accesses it through defined business operations, not model-generated commands.
- **Derived views:** analytical views, if introduced, cannot authorize workflow updates.
- **Access control:** the note also made the application responsible for controlling who may read or change a case. Its design is accepted in [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) and implemented at the application layer for case creation, reading, and listing (see [Current implementation](#current-implementation)). Access control for status changes is not implemented.

> Original rationale: "Case ownership, authorization, and status must remain consistent and auditable even when AI is unavailable."

### DEC-002 — The AI component produces suggestions, not business actions

Formalized in [ADR-0002](adr/0002-ai-suggestions-non-authoritative.md).

- **What the AI does:** it receives only the case information the application permits, and suggests a category, urgency, explanation, and next step.
- **How output is handled:** each response is validated against a structured contract and stored with its model and prompt provenance.
- **What the AI cannot do:** it does not approve changes, assign permissions, update case status, or write to the operational database.

> Original rationale: "Model responses can be incomplete, incorrect, or inconsistent. They are useful inputs for review, not trusted instructions."

### DEC-003 — Only authorized application operations can change case state

Formalized in [ADR-0003](adr/0003-application-controlled-case-transitions.md).

- **Rules and authorization:** case state changes only through application operations that apply explicit transition rules and check the required authorization.
- **Approval binding:** where human approval is required, the application verifies that the approval applies to the exact proposed change.
- **Separate steps:** accepting a suggestion and executing a state change are separate steps.
- **What is implemented:** the transition rules and version checks are implemented. Authorization of transitions and approval binding are accepted but not yet implemented.

> Original rationale: "This prevents a generated response, stale suggestion, or repeated request from silently becoming an unauthorized business action."

## Current implementation

The initial schema is implemented in [`db/001_init.sql`](../db/001_init.sql), and its relational integrity checks pass ([11/11](../tests/schema-integrity-results.md)).

The initial local [AI triage CLI](ai-triage-integration.md) follows the suggestion-only write path described in [ADR-0002](adr/0002-ai-suggestions-non-authoritative.md) and [ADR-0003](adr/0003-application-controlled-case-transitions.md):

1. The model supplies a recommendation.
2. Python validates it against a Pydantic output contract.
3. Application-controlled code rechecks the case state and inserts the recommendation into `ai_suggestion`.
4. In the [recorded run](../evaluations/model-run-01.json), the source case status and version remained unchanged (`NEW`, version `1`, before and after the write).

The model has no database access; only application code writes.

A local [case service](case-operations.md) ([`src/case_service.py`](../src/case_service.py)) implements the transition-control part of [ADR-0003](adr/0003-application-controlled-case-transitions.md). Its [validation tests passed (16/16)](../tests/case-operations-results.md).

1. Transitions follow an initial forward-only policy: `NEW` → `IN_REVIEW` → `RESOLVED` → `CLOSED`. Skipped, backward, no-op, and post-closure transitions are rejected.
2. Each transition locks the case row, compares the caller's expected `version`, and increments it with the status change, so a stale request fails instead of overwriting newer state.
3. The status update and a `case_status_event` row ([`db/002_case_status_events.sql`](../db/002_case_status_events.sql)) are written in one transaction; if the event cannot be recorded, the status change is rolled back.
4. AI suggestions are not an input to the transition operation.

**What the service does not do:**

- **No authorization check.** The organization ID it receives filters queries but is supplied by the caller, and the recorded `actor_label` is not a verified identity or approval.
- **Partial history.** The event table records status transitions made through the service only. It is not a complete or tamper-proof audit trail, and database clients with write access can bypass the service.
- **Keyed idempotent creation.** Requests with an `Idempotency-Key` are deduplicated per authorized organization and verified actor ([ADR-0004](adr/0004-keyed-idempotent-case-creation.md); [results](../tests/keyed-case-creation-results.md)). Requests without a key, and the local `create_case` service, still insert a new case each time. Idempotency records never expire in the local demonstration.

Actor identity and customer access grants ([`db/003_actor_identity_and_grants.sql`](../db/003_actor_identity_and_grants.sql)) implement the data side of [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) and the eligibility rules of [ADR-0006](adr/0006-actor-affiliation-and-role-grant-eligibility.md). PostgreSQL guards reject ineligible grants and affiliation changes, and trusted local provisioning ([`src/identity_provisioning.py`](../src/identity_provisioning.py)) locks the actor first. Schema, eligibility, and concurrency tests passed locally ([87/87](../tests/identity-grant-integrity-results.md)). A local credential resolver ([`src/credential_auth.py`](../src/credential_auth.py)) verifies opaque test credentials against a git-ignored digest store and returns a verified `actor.id` ([58/58 tests](../tests/credential-verification-results.md)). The authorized-case-operations layer ([`src/authorized_case_operations.py`](../src/authorized_case_operations.py)) is the single entry point for protected create, read, and list. It resolves the credential, enforces the ADR-0005 matrix with locked create transactions and scoped single-statement reads, and is guarded by a structural no-bypass check ([76/76 tests](../tests/case-authorization-results.md)). A localhost-only HTTP API ([`src/api/`](../src/api/)) calls it for every route ([HTTP results](../tests/http-case-api-results.md)), including keyed idempotent creation ([results](../tests/keyed-case-creation-results.md)). A superuser, a direct SQL client, or a writer that disables triggers can still bypass both the layer and the guards.

## Current boundary

The CLI, the case service, and the HTTP API remain local development tools. Initial output-validation and [failure-path tests](../tests/ai-triage-failure-results.md) have passed, and the recorded tests establish local, synthetic behavior only. They do not establish production-grade fault tolerance, production authentication, database-enforced tenant isolation, human approval or approval binding, or end-to-end recovery guarantees. The case service and the CLI have no authentication or authorization of their own, and the case service's `create_case` is not idempotent. Those controls will be verified as their corresponding features are built.
