# ADR-0003: Application-Controlled Case State Transitions

- **Status:** Accepted
- **Recorded:** 2026-11-13
- **Decision provenance:** Retrospective reconstruction.
  - This ADR formalizes the earlier note [DEC-003](../architecture-context.md#dec-003--only-authorized-application-operations-can-change-case-state), "Only authorized application operations can change case state", first committed on 2026-11-11.
  - The transition policy, version checks, and status-event table were implemented on 2026-11-13.
  - The options below were reconstructed for transparent comparison; they were not formally evaluated at the time.
- **Legacy reference:** DEC-003
- **Related requirements:** FR-02, FR-03, FR-06, FR-07, FR-09; rules "Human authorization", "Concurrency", and "Auditability" ([requirements](../requirements.md))
- **Implementation evidence:**
  - Source and migration: [`src/case_service.py`](../../src/case_service.py), [`db/002_case_status_events.sql`](../../db/002_case_status_events.sql).
  - Design: [controlled case operations](../case-operations.md).
  - Results: [case operations validation](../../tests/case-operations-results.md) (16/16 tests passed).

## Problem

The database restricts `case_record.status` to valid values, but a valid value does not prove that a transition is permitted. A direct update can move a case from `NEW` to `CLOSED`. Concurrent or stale requests can overwrite newer state, and a status change saved without its history record leaves the history inconsistent. A model suggestion or a repeated request must not silently become a state change.

## Constraints

- **Existing decisions.** PostgreSQL is the authoritative store ([ADR-0001](0001-postgresql-authoritative-case-store.md)), and AI output is non-authoritative ([ADR-0002](0002-ai-suggestions-non-authoritative.md)).
- **Unsettled business rules.** Rules for reopening cases, and for which roles may resolve or close them, are still open ([requirements](../requirements.md#10-open-decisions)).
- **No identity system.** User authentication and identity do not exist yet.
- **Unmeasured concurrency.** There is a single local database, and expected concurrency has not been measured.

## Options Considered

1. **An application case service using PostgreSQL transactions**
   - *Benefits:* the policy is explicit in one place and can be tested without a database. Version checks and row locks prevent lost updates, and the event is written in the same transaction as the status change.
   - *Costs:* only callers that use the service are governed by it. Database clients with write access can bypass it.
2. **Unrestricted direct SQL status updates**
   - *Benefits:* the simplest implementation.
   - *Costs:* no transition policy, no history, and lost updates under concurrency. Business rules are easily bypassed.
3. **Transition enforcement in the database, through triggers or stored procedures**
   - *Benefits:* enforcement applies to every writer, including ad-hoc SQL.
   - *Costs:* the policy is split between SQL and application code, or duplicated in both. It is harder to test and change while business rules are still unsettled, and adds database-specific logic. Authorization would still have to come from the application.

## Decision

Case state changes only through application operations that apply explicit rules. Option 1 is adopted. The decision has two parts.

**Transition control, enforced by the local case service:**

- **Policy.** The initial policy is `NEW → IN_REVIEW → RESOLVED → CLOSED`. Skipped, backward, no-op, post-closure, and unknown transitions are rejected.
- **Locking and versions.** The service locks the case row (`SELECT ... FOR UPDATE`), compares the caller's expected `version`, and increments the version atomically with the status change.
- **History in the same transaction.** It inserts a `case_status_event` row in the same transaction. If the event cannot be recorded, the status update rolls back.
- **No AI input.** AI suggestions are not an input to a transition.

**Authorization and approval, accepted from DEC-003 but not yet implemented:**

- The caller's identity and tenant permissions must be checked before a transition is executed.
- Where human approval is required, the application must verify that the approval applies to the exact proposed change (case, version, and target state) before executing it.
- Accepting a suggestion and executing a state change remain separate steps.

**Critical distinction.** When this ADR was recorded, the case service enforced only the transition policy and the concurrency rules. **It did not authenticate the caller or verify tenant permissions.**
- `organization_id` scopes the SQL queries but is supplied by the caller.
- `actor_label` is caller-supplied text, not a verified user identity or approval.

The implementation therefore does not yet satisfy DEC-003's authorization and approval requirements, and the service must not be exposed to untrusted callers.

Option 1 was chosen because it keeps the transition rules explicit and testable while the business rules are still changing, and PostgreSQL transactions provide the consistency guarantees.

## Risks and Consequences

- **Bypass by direct writers.** No trigger enforces the policy, and the development account can write directly to the database.
- **Incomplete history.** `case_status_event` records only status transitions made through the service. It does not record creation, content edits, reviews, or rejected attempts. It is not a complete audit log and is not tamper-proof.
- **No verified attribution or approval binding.** No transition can yet be attributed to an authenticated person, or tied to a recorded approval.
- **Lock contention.** Row locks serialize writers on a single case, so transactions must stay short; in particular, no model call may run inside one. Contention has not been measured.
- **Changing the rules.** Reopening or additional states would change the policy and its tests. Moving enforcement elsewhere, for example into the database, requires a superseding ADR.
- **Obligations for new writers.** Any new service that changes case state must use this operation, or provide equivalent guarantees: locking, a version check, history recorded in the same transaction, and authorization.

## Revisit When

- A second writer to `case_record` is introduced.
- The application exposes state-changing endpoints to users or other services.
- Reopening or additional states become business requirements.
- Transaction measurements show unacceptable lock contention.
- Enforcement against writers that bypass the service becomes necessary, which favors a stronger database-enforced policy.
