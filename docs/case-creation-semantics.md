# Case Creation Semantics

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** This document records current behavior and a proposed future contract. **Idempotent case creation is not implemented.** The contract below is a proposed design decision for a future create-case API, not a current guarantee. No database-backed duplicate-creation or idempotency test has been executed.  
**Decision record:** [ADR-0004](adr/0004-keyed-idempotent-case-creation.md) (Proposed; revised to an actor- and organization-scoped key) records this decision, the alternatives considered, and the tradeoffs. Identity and authorization are proposed in [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md). Neither ADR is accepted or implemented. This document remains the detailed design reference for current behavior, contract rules, expected outcomes, open decisions, and required validation.

## 1. The problem

A request to create a case can fail in a way that leaves the caller unsure whether the case was created. For example, the connection drops after the database commits but before the response arrives. A caller that retries may then create a second case for the same incident. Duplicate cases split the case history, duplicate triage work, and can lead to conflicting decisions.

The [requirements](requirements.md) state that reprocessing must not create unintended duplicate business effects (rule "Retries") and that duplicate requests must be tested (quality requirement "Correctness").

Case content cannot identify a duplicate request, however. Two genuinely separate incidents can have the same title and description, for example a recurring VPN outage reported twice. Rejecting the second as a duplicate would lose a real case.

## 2. Current behavior

### 2.1 `create_case()`

[`create_case()`](../src/case_service.py) in the local case service:

1. Validates that `organization_id` is a UUID and that `title` and `description` are nonblank text.
2. Checks that the organization exists.
3. Inserts a new `case_record` row. The database generates a new `id` and applies the defaults `status = 'NEW'` and `version = 1`.
4. Returns the inserted row.

**Each successful call performs a fresh insert.** The function takes no request identifier, does not look for an earlier matching request, and does not compare content with existing cases. It does not implement request idempotency. If a caller retries after an ambiguous failure, the retry creates a second case with a different ID.

The schema does not prevent this either. [`db/001_init.sql`](../db/001_init.sql) defines no uniqueness constraint on `case_record` beyond its primary key, and [`db/002_case_status_events.sql`](../db/002_case_status_events.sql) does not affect case creation. Case creation does not record a status event.

### 2.2 Development seed script

[`db/seed_dev.sql`](../db/seed_dev.sql) avoids some duplicates when it is rerun:

- Organizations use `ON CONFLICT (slug) DO NOTHING`, which relies on the unique `organization.slug` constraint.
- Each sample case is inserted only `WHERE NOT EXISTS` a case with the same title for the same organization.

**This is development-data initialization behavior, not an application idempotency guarantee:**

- It identifies duplicates by **title**. That is content matching, which this design rejects for real requests because it would suppress a genuine second case with the same title.
- It is **not safe under concurrency**. No uniqueness constraint backs the `NOT EXISTS` check, so two sessions running the script at the same time can both find no match and both insert.
- It applies only to the seed script. `create_case()` does not use it.

The AI triage CLI reads the earliest case with the seed title (`ORDER BY c.created_at ASC, c.id ASC LIMIT 1` in [`scripts/run_triage.py`](../scripts/run_triage.py)). It tolerates duplicate seed cases; it does not prevent them.

### 2.3 Test coverage

The [case operations validation](../tests/case-operations-results.md) (16/16 passed) includes two creation tests:

- `test_create_and_read_case` creates and reads back one case.
- `test_invalid_create_input_rejected` rejects blank text.

Neither submits the same creation request twice. **No database-backed duplicate-creation or idempotency test has been executed**, and the recorded results make no claim about duplicate handling.

## 3. Design decision: distinct creation by default, idempotent creation by key

**Decision status:** Proposed in [ADR-0004](adr/0004-keyed-idempotent-case-creation.md) as the preferred design direction for a future create-case API. **Not implemented.**

| # | Rule |
|---|---|
| 1 | **A genuinely new request creates a new case**, even when another case has the same title or description. Content is not request identity. No uniqueness constraint will be added on case title or description. |
| 2 | **An accidental retry must not create a duplicate business case** once idempotent creation is implemented. |
| 3 | **A future create-case API accepts an optional client-provided `Idempotency-Key` header.** The client generates a unique, high-entropy key (for example, a UUIDv4) for each intended case and reuses it only when retrying that same request. The proposed format is case-sensitive `[A-Za-z0-9._~-]{16,128}`. A malformed key is rejected with `400`. Raw keys are not logged. |
| 4 | **Authorization runs before any key lookup, and the key is scoped to `(authorized organization_id, verified actor_id, 'case:create', exact key)`.** The actor comes from server-side credential verification and the organization from a successful `case:create` decision ([ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md), Proposed). Neither is ever taken from the client unchecked. The same literal key used by another actor or in another organization is an **independent operation** that cannot replay or conflict with this one. A denied request learns nothing about stored keys. |
| 5 | **Same scope, equivalent payload: a replay.** No new case is created. The response is `200 OK` with `Location`, `Idempotency-Replayed: true`, and a minimal body containing only the immutable identity `id` and `organization_id`, taken from the idempotency record. It is **not** the case's current state, and it is not a `case:read` grant. |
| 6 | **Same scope, different payload: a conflict.** The response is `409 Conflict`. No case is created or modified, and no detail of the first request is returned. |
| 7 | **Concurrent requests in the same scope must not create multiple cases.** The database uniqueness constraint serializes them, and the later request returns the replay or conflict outcome. If a bounded wait cannot establish the outcome, the response is a generic `503`, and the client may safely retry with the same key. |
| 8 | **The idempotency record is stored atomically with case creation.** The scope, the request fingerprint, and the resulting case ID commit in the same transaction as the new case. A case created through a keyed request never exists without its record, and a record never refers to a case that was rolled back. |
| 9 | **Requests without a key keep distinct-creation semantics:** each successful request creates a new case. This is documented behavior, not an accident, and it remains until an API contract requires keys. |
| 10 | **Equivalence uses a request fingerprint.** The fingerprint is SHA-256 over the [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) canonical JSON of `{"organization_id", "title", "description"}`. The UUID is in lowercase hyphenated form; the text is exactly as validated and persisted, with no trimming, case folding, or Unicode normalization. Key order and incidental JSON formatting do not affect it. Unknown body fields are rejected before the fingerprint is computed. |
| 11 | **For the local synthetic demonstration, idempotency records do not expire.** This is a bounded demonstration tradeoff, not a production retention policy. |

### Expected outcomes

| Scenario | Outcome under the proposed contract |
|---|---|
| Two requests without a key, identical content | Two distinct cases |
| Two requests with different keys, identical content | Two distinct cases |
| Retry by the same actor with the same key and an equivalent payload, after the first request committed | `200` replay with identity only (`id`, `organization_id`); one case in total |
| Same actor, same key, materially different payload | `409` conflict; the original case is unchanged; one case in total |
| Two simultaneous requests from the same actor with the same key | Exactly one case; the later request replays, conflicts, or gets `503` if its bounded wait expires |
| Two actors in one organization use the same key, with the same or a different payload | Independent operations; one case each; no replay, and no conflict between them |
| The same actor uses the same key in two organizations where it is a member | Independent operations; one case for each organization |
| A requester replays its key after the case was changed by someone else | `200` replay with identity only; the changes are not disclosed |
| A replay arrives after the actor's membership was revoked | Denied before any key lookup; nothing about the key is revealed |
| The actor's credential is rotated, but still resolves to the same `actor.id` | The retry replays normally |
| First attempt rolled back before commit, then retried with the same key | No record was committed, so the retry creates the case once |

### Illustrative transaction

This sketch is a **design aid only**. It is not a migration, it has not been executed, and no `case_creation_idempotency` table exists.

```sql
-- Proposed table shape (not created):
--   case_creation_idempotency(organization_id, actor_id, operation, idempotency_key,
--                             request_fingerprint, case_id NOT NULL REFERENCES case_record,
--                             created_at)
--   UNIQUE (organization_id, actor_id, operation, idempotency_key)
-- Preconditions: credential verified, actor active, membership active, case:create granted.
BEGIN;
INSERT INTO case_record (organization_id, title, description)
VALUES ($authorized_org, $title, $description)
RETURNING id;                                         -- $new_case_id
INSERT INTO case_creation_idempotency
       (organization_id, actor_id, operation, idempotency_key, request_fingerprint, case_id)
VALUES ($authorized_org, $verified_actor, 'case:create', $key, $fingerprint, $new_case_id)
ON CONFLICT (organization_id, actor_id, operation, idempotency_key) DO NOTHING
RETURNING case_id;
-- One row returned:  COMMIT, then 201 Created with the full creation representation.
-- No row returned:   ROLLBACK (discarding the new case), then read the committed record
--                    in the same scope: equal fingerprint -> 200 replay (id, organization_id);
--                    different fingerprint -> 409.
```

Under READ COMMITTED, the second `INSERT` waits if a concurrent transaction holds an uncommitted record with the same scope. When that transaction finishes, the `INSERT` either returns no row (the other transaction committed) or proceeds (it rolled back). The wait needs a bound, for example a lock or statement timeout. No value has been chosen; it should be set from measurement. If the bound expires, the transaction rolls back and the response is a generic `503`. The case insert therefore commits only when its record does, and a request that loses a race leaves no case behind.

## 4. Open implementation decisions

[ADR-0004](adr/0004-keyed-idempotent-case-creation.md) now **proposes** answers to the earlier open questions:

- key scope;
- replay status and content;
- conflict status;
- in-flight behavior;
- key format;
- fingerprint;
- demonstration retention.

These remain proposals until the architecture owner accepts them. Still open:

- **Status codes versus the IETF draft.** The [Idempotency-Key header field draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) recommends `422` for a payload mismatch and `409` for an in-flight retry. This contract proposes `409`, and wait-then-`503`. The architecture owner must confirm or change this.
- **Bounded wait.** The timeout value and mechanism, chosen from measurement, and whether a `503` carries `Retry-After`.
- **Fingerprint serialization.** Confirm RFC 8785 as the exact canonical form. Whether to store only the digest, or the canonical payload as well.
- **Production retention.** A finite retention period, and how a key reused after expiry is treated. Both are required before any use beyond the local demonstration.
- **Key requirement.** Whether some clients will be required to send a key.
- **Shared clients.** Whether service principals or shared clients ever need deduplication across actors. That would need a separate design for who owns an operation.

## 5. Validation required before a retriable create-case API is enabled

A separate validation increment must test the implementation against a dedicated test database and record observed results. As a minimum, it must cover:

1. **Sequential replay by the same actor.** The same key and payload are submitted twice: exactly one case exists, the first response is `201`, and the second is a `200` replay containing identity only.
2. **Simultaneous requests in the same scope.** They produce exactly one case and one idempotency record.
3. **Same scope, different payload.** The second request gets `409`, and the first case is unchanged.
4. **Distinct keys with identical case content.** Two distinct cases are created.
5. **Two actors, one organization, same key.** With the same payload and with a changed payload, the requests are independent: two cases, no replay, no conflict.
6. **Same actor in two organizations.** The same key used in each creates one case per organization.
7. **Cross-organization key isolation.** The same key value used in different organizations creates independent cases.
8. **Requester replay after the case changed.** The replay contains only `id` and `organization_id`, with no current state.
9. **Revocation.** A replay after the membership or actor was deactivated is denied before any key lookup.
10. **Credential rotation.** A rotated credential that resolves to the same `actor.id` replays normally.
11. **Rollback after failure.** After a simulated failure following the case insert, neither the case nor the idempotency record persists.
12. **Ambiguous outcome.** If the bounded wait expires, the response is `503` and nothing is inserted by that request. A retry with the same key then resolves to a replay or a conflict.
13. **No key.** Requests without a key create distinct cases.

The same list appears in [ADR-0004](adr/0004-keyed-idempotent-case-creation.md#revisit-when). These tests are required before keyed creation is **enabled**. They are not prerequisites for accepting the ADR as a design.

None of these tests exists yet. Until observed results are recorded, no document should claim that case creation is idempotent or duplicate-safe.

## 6. Related documents

- [ADR-0004: Keyed Idempotent Case Creation](adr/0004-keyed-idempotent-case-creation.md): the proposed decision and its tradeoffs.
- [System context](system-context.md): current components, capability status, and trust boundaries.
- [Controlled case operations](case-operations.md): the local case service and its limits.
- [Data model](data-model.md): `case_record` attributes and integrity rules.
- [Requirements](requirements.md): the "Retries" rule and the "Correctness" quality requirement.
