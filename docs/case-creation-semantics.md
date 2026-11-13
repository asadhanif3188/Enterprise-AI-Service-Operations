# Case Creation Semantics

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** This document records current behavior and a proposed future contract. **Idempotent case creation is not implemented.** The contract below is a design decision for a future create-case API, not a current guarantee. No database-backed duplicate-creation or idempotency test has been executed.

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

**Decision status:** Adopted as the design direction for a future create-case API. **Not implemented.**

| # | Rule |
|---|---|
| 1 | **A genuinely new request creates a new case**, even when another case has the same title or description. Content is not request identity. No uniqueness constraint will be added on case title or description. |
| 2 | **An accidental retry must not create a duplicate business case** once idempotent creation is implemented. |
| 3 | **A future create-case API accepts a client-provided idempotency key.** The client generates a unique key for each intended case and reuses it only when retrying that same request. How the key is transported (for example, a request header) is an API design decision. |
| 4 | **The key is scoped to the owning organization and the create-case operation.** The same key value used for a different organization or a different operation is a separate identity. Once tenant authorization exists, the scope must use the organization the caller is authorized for, not an unchecked caller-supplied ID. Otherwise one tenant could probe or collide with another tenant's keys. |
| 5 | **The same key with an equivalent payload returns the previously created case.** No new case is created. |
| 6 | **Reusing a key with a materially different payload is rejected as a conflict.** No case is created or modified. |
| 7 | **Concurrent requests with the same key must not create multiple cases.** At most one case results, regardless of timing. |
| 8 | **The idempotency record is stored atomically with case creation.** The key, its scope, a payload fingerprint, and the resulting case ID are committed in the same transaction as the new case. A case created through a keyed request never exists without its record, and a record never refers to a case that was rolled back. |
| 9 | **Requests without a key keep distinct-creation semantics:** each successful request creates a new case. This is documented behavior, not an accident, and it remains until an API contract requires keys. |

### Expected outcomes

| Scenario | Outcome under the proposed contract |
|---|---|
| Two requests without a key, identical content | Two distinct cases |
| Two requests with different keys, identical content | Two distinct cases |
| Retry with the same key and an equivalent payload after the first request committed | The original case is returned; one case in total |
| Same key, materially different payload | Conflict; the original case is unchanged; one case in total |
| Two simultaneous requests with the same key | Exactly one case |
| The same key value used by two different organizations | Independent requests; one case for each organization |
| First attempt rolled back before commit, then retried with the same key | No record was committed, so the retry creates the case once |

### Illustrative implementation direction

The following is an option to evaluate, not a decision, and no migration for it exists. An idempotency table could hold a uniqueness constraint over (organization, operation, key), a payload fingerprint, and the created case ID. Each keyed request would insert its record in the same transaction as the case. The uniqueness constraint, rather than an application-level check, would resolve concurrent requests with the same key. The losing request then reads the committed record and either returns the original case or reports a conflict.

## 4. Open implementation decisions

These must be decided explicitly before idempotent creation is built:

- **Key retention period.** How long a key is remembered, and how a key reused after expiry is treated.
- **Canonical payload definition.** Which fields make two payloads equivalent (for example, organization, title, and description), and which normalization applies (whitespace, Unicode normalization, letter case). Also whether the full payload or only a fingerprint is stored.
- **Retry-response contract.**
  - The response returned for a replayed request, and whether it indicates a replay.
  - The error returned for a key conflict.
  - The response a request receives while another request with the same key is still in progress.
- **Replay content.** Whether a replay returns the case as originally created or in its current state, since the case may have changed since creation.
- **Key format.** Allowed length, character set, and validation.
- **Key requirement.** Whether some clients will be required to send a key.

## 5. Validation required before a retriable create-case API is enabled

A separate validation increment must test the implementation against a dedicated test database and record observed results. As a minimum, it must cover:

1. **Sequential duplicate submissions.** The same key and payload are submitted twice; exactly one case exists, and both responses identify it.
2. **Simultaneous same-key requests.** Concurrent requests with the same key produce exactly one case and one idempotency record.
3. **Same key, different payload.** The second request is rejected as a conflict, and the first case is unchanged.
4. **Distinct keys with identical case content.** Two distinct cases are created.

Further cases worth covering include:

- Requests without a key creating distinct cases.
- The same key value used by different organizations.
- A simulated failure after the case insert, after which neither the case nor the idempotency record persists.

None of these tests exists yet. Until observed results are recorded, no document should claim that case creation is idempotent or duplicate-safe.

## 6. Related documents

- [System context](system-context.md): current components, capability status, and trust boundaries.
- [Controlled case operations](case-operations.md): the local case service and its limits.
- [Data model](data-model.md): `case_record` attributes and integrity rules.
- [Requirements](requirements.md): the "Retries" rule and the "Correctness" quality requirement.
