# Architecture Context

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** Initial architecture decision baseline

This document records the starting boundaries between the application and its AI component. These decisions guide the first implementation and may be revisited when requirements or evidence change. The [system context](system-context.md) shows the current components, capability status, and trust boundaries.

## DEC-001 — The application owns service-case data and workflow decisions

The application is responsible for creating and storing service cases, identifying their owning organization, enforcing valid state transitions, and recording who made each decision. It also controls which users and services are allowed to read or change a case.

PostgreSQL has been selected as the initial system of record. The current schema stores organizations, service cases, AI suggestion records, and case status-transition events. A complete case history and review outcomes will be modeled in later increments. The application will access that data through defined business operations, not through model-generated commands.

**Why:** Case ownership, authorization, and status must remain consistent and auditable even when AI is unavailable.

## DEC-002 — The AI component produces suggestions, not business actions

The AI component receives the case information that the application permits it to use. It can suggest a category, urgency, explanation, and next step. Its response must be validated against an expected structure and recorded with enough information to identify the model and prompt version.

The AI component does not approve changes, assign permissions, update case status, or directly write to the operational database.

**Why:** Model responses can be incomplete, incorrect, or inconsistent. They are useful inputs for review, not trusted instructions.

## DEC-003 — Only authorized application operations can change case state

A model's recommendation does not determine whether a case is new, in review, resolved, or closed. Case state changes only when application rules permit the transition and the required authorization has been checked. Where human approval is required, the application must verify that the approval still applies to the exact proposed change.

An AI suggestion may inform a person's decision, but accepting a suggestion and executing a state change are separate steps.

**Why:** This prevents a generated response, stale suggestion, or repeated request from silently becoming an unauthorized business action.

---

**Current implementation:** The initial schema is implemented in [`db/001_init.sql`](../db/001_init.sql), and its relational integrity checks pass ([11/11](../tests/schema-integrity-results.md)). The initial local [AI triage CLI](ai-triage-integration.md) follows the suggestion-only write path described in DEC-002 and DEC-003:

1. The model supplies a recommendation.
2. Python validates it against a Pydantic output contract.
3. Application-controlled code rechecks the case state and inserts the recommendation into `ai_suggestion`.
4. In the [recorded run](../evaluations/model-run-01.json), the source case status and version remained unchanged (`NEW`, version `1`, before and after the write).

The model has no database access; only application code writes.

A local [case service](case-operations.md) ([`src/case_service.py`](../src/case_service.py)) implements the application-rule part of DEC-003. Its [validation tests passed (16/16)](../tests/case-operations-results.md).

1. Transitions follow an initial forward-only policy: `NEW` → `IN_REVIEW` → `RESOLVED` → `CLOSED`. Skipped, backward, no-op, and post-closure transitions are rejected.
2. Each transition locks the case row, compares the caller's expected `version`, and increments it with the status change, so a stale request fails instead of overwriting newer state.
3. The status update and a `case_status_event` row ([`db/002_case_status_events.sql`](../db/002_case_status_events.sql)) are written in one transaction; if the event cannot be recorded, the status change is rolled back.
4. AI suggestions are not an input to the transition operation.

This service does **not** check authorization. The organization ID it receives filters queries but is supplied by the caller, and the recorded `actor_label` is not a verified identity or approval. The event table records status transitions made through the service only; it is not a complete or tamper-proof audit trail, and database clients with write access can bypass the service. Case creation is not idempotent: each successful `create_case` call inserts a new case. The proposed idempotency-key contract is described in [Case creation semantics](case-creation-semantics.md).

**Current boundary:** The CLI and the case service remain local development tools. Initial output-validation and [failure-path tests](../tests/ai-triage-failure-results.md) have passed, but these do not establish production-grade fault tolerance, authentication, tenant authorization, approval binding, or end-to-end recovery guarantees. Those controls will be verified as their corresponding features are built.
