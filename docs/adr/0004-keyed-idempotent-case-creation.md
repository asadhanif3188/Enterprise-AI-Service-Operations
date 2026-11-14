# ADR-0004: Keyed Idempotent Case Creation

- **Status:** Proposed
- **Recorded:** 2026-11-13
- **Decision provenance:** Contemporary proposal. It formalizes the design direction first documented in [Case creation semantics, section 3](../case-creation-semantics.md#3-design-decision-distinct-creation-by-default-idempotent-creation-by-key) on 2026-11-13. **Idempotent creation is not implemented, and no idempotency tests exist.**
- **Legacy reference:** [Case creation semantics](../case-creation-semantics.md), section 3
- **Related requirements:** FR-01, FR-11; rules "Retries" and "Concurrency"; quality requirement "Correctness" ([requirements](../requirements.md))
- **Implementation evidence:** None for idempotent creation.
  - [`src/case_service.py`](../../src/case_service.py): `create_case()` inserts a new case on every successful call.
  - [`db/001_init.sql`](../../db/001_init.sql): defines no uniqueness constraint on `case_record` beyond its primary key.

## Problem

A case-creation request can commit even though the client never receives the response, for example when the connection drops after the commit. If the client retries, a second case is created for the same intended operation. Two legitimate incidents, however, can have identical titles and descriptions. Content equality is therefore not the same as operation identity, and duplicates cannot be detected by comparing case content.

## Constraints

- **No API yet.** No application API exists. The contract must be settled before any create-case operation is offered to clients that retry.
- **Content is not identity.** Legitimate cases may share the same content, so no uniqueness constraint can be placed on title or description.
- **No authenticated organization.** Authentication and tenant authorization do not exist, so the organization in any key scope is currently supplied by the caller.
- **Concurrency.** Concurrent requests carrying the same key must be safe, with PostgreSQL as the authority ([ADR-0001](0001-postgresql-authoritative-case-store.md)).
- **Seed data is not evidence.** The development seed script's duplicate checks are setup behavior, not an application mechanism.

## Options Considered

1. **Organization-scoped idempotency key (preferred)**
   - *Benefits:* the client supplies a stable key that identifies one intended create operation, and reuses it only when retrying. Storing the key and the resulting case atomically, under a database uniqueness guarantee, makes retries and concurrent duplicates safe without judging content.
   - *Costs:* clients must generate and keep keys. Key records must be stored and eventually expired, and payload equivalence must be defined.
2. **Deduplication by title and description**
   - *Benefits:* easy to understand, and no client changes are needed.
   - *Costs:* it treats legitimate repeated incidents as retries and silently loses real cases. It is unsafe as a general business rule.
3. **Always create a new case (current behavior)**
   - *Benefits:* simple; already implemented.
   - *Costs:* no protection against duplicate cases after an ambiguous failure and a retry.

## Decision

**Proposed:** adopt option 1. The detailed rules, expected outcomes, and open questions are in [Case creation semantics](../case-creation-semantics.md); this ADR records the decision and its tradeoffs.

- **New requests.** A genuinely new creation request creates a new case.
- **Retry identity.** A client-provided idempotency key identifies a retryable operation.
- **Key scope.** Keys are scoped by the owning organization and the create-case operation. Once tenant authorization exists, the scope must use the organization the caller is authorized for.
- **Same key, equivalent payload.** The request returns the original case.
- **Same key, materially different payload.** The request is rejected as a conflict.
- **Concurrent requests.** Concurrent requests with the same key create at most one case.
- **Atomicity.** The idempotency record and the created case commit atomically.
- **Matching content.** Identical titles or descriptions are not grounds for deduplication.
- **Requests without a key.** These keep distinct-creation semantics until a different API policy is explicitly accepted.

Option 1 is preferred because it prevents duplicates from retries without rejecting legitimate cases that happen to share content.

**Implementation boundary at the time of recording:**
- `create_case()` inserts a new record on every successful call; **idempotent creation is not implemented**.
- The seed script's duplicate avoidance does not provide application-level idempotency.
- No duplicate-creation result can be inferred from the existing case-service tests.

## Risks and Consequences

- **Client responsibility.** Clients must generate unique keys and reuse a key only for the same intended case. A key reused for a different case produces a conflict.
- **Storage and retention.** Idempotency records need storage and a retention and cleanup policy, and a key reused after expiry needs defined behavior.
- **Equivalence errors.** An incorrect definition of payload equivalence could reject genuine retries, or accept a changed payload as a replay.
- **Trust in the scope.** Until tenant authorization exists, an organization-scoped key is only as trustworthy as the caller-supplied organization ID.
- **Requests without keys.** These remain non-idempotent by design.

**Open questions, deliberately not settled here:**

- Key retention period.
- Key validation and format.
- Payload canonicalization: which fields define equivalence, and how they are normalized.
- Retry-response semantics: the status or result returned for a replay.
- Replay content: the case as originally created, or its current state.
- Handling of a request that arrives while another request with the same key is still in progress.
- Whether keys become mandatory for particular clients.

## Revisit When

- **Before Accepted.** The open questions above must be resolved before this ADR moves to Accepted.
- **Before any retriable create-case API** (for example `POST /cases`; no API has been designed), observed tests against a dedicated test database must cover:
  1. Sequential replay with the same key and payload.
  2. Concurrent requests with the same key.
  3. The same key with a different payload.
  4. Different keys with identical payloads.
  5. Isolation of keys across organizations.
  6. Rollback of both the case and the idempotency record after a simulated failure.
- **If the contract changes.** If an API design requires keys for all clients, or changes the key scope, this ADR is revised while Proposed, or superseded once Accepted.
