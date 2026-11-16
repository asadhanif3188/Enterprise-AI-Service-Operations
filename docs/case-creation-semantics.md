# Case Creation Semantics

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** **Keyed idempotent case creation is implemented** for the local demonstration, as accepted in ADR-0004: migration [`db/004_case_creation_idempotency.sql`](../db/004_case_creation_idempotency.sql), `create_case_keyed` in the [authorized case operations](../src/authorized_case_operations.py), and `POST /cases` with an `Idempotency-Key` header in the [local HTTP API](../src/api/). Observed results: [keyed case creation results](../tests/keyed-case-creation-results.md). Requests **without** a key still create distinct cases, and the local [`create_case()`](../src/case_service.py) service remains non-idempotent.  
**Decision record:** [ADR-0004](adr/0004-keyed-idempotent-case-creation.md) (**Accepted** 2026-11-15; implemented 2026-11-16) records this decision, the alternatives considered, and the tradeoffs. Identity and authorization come from [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) (Accepted; implemented in the Python authorization layer and the local HTTP API). This document remains the detailed design reference for behavior, contract rules, expected outcomes, and validation.

## 1. The problem

A request to create a case can fail in a way that leaves the caller unsure whether the case was created. For example, the connection drops after the database commits but before the response arrives. A caller that retries may then create a second case for the same incident. Duplicate cases split the case history, duplicate triage work, and can lead to conflicting decisions.

The [requirements](requirements.md) state that reprocessing must not create unintended duplicate business effects (rule "Retries") and that duplicate requests must be tested (quality requirement "Correctness").

Case content cannot identify a duplicate request, however. Two genuinely separate incidents can have the same title and description, for example a recurring VPN outage reported twice. Rejecting the second as a duplicate would lose a real case.

## 2. Current behavior

### 2.1 `create_case()` (local case service)

> The HTTP API does not use this function. Its create path is the authorized case operations, which offer unkeyed creation (distinct cases) and keyed creation (section 3).

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

**Decision status:** **Accepted** as architecture in [ADR-0004](adr/0004-keyed-idempotent-case-creation.md) on 2026-11-15.

- **Implemented.** Keyed creation is enabled in the local HTTP API. The tests in section 5 were executed against PostgreSQL ([results](../tests/keyed-case-creation-results.md)).
- **Dependency.** Verified identity and authorization come from [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md), implemented in the authorized case operations; authorization runs before every key lookup.
- **Unchanged.** Requests without a key, and the local `create_case()` service, keep distinct-creation semantics.

| # | Rule |
|---|---|
| 1 | **A genuinely new request creates a new case**, even when another case has the same title or description. Content is not request identity. No uniqueness constraint will be added on case title or description. |
| 2 | **An accidental retry must not create a duplicate business case** once idempotent creation is implemented. |
| 3 | **An optional `Idempotency-Key` header, parsed as an [RFC 9651](https://www.rfc-editor.org/rfc/rfc9651#section-3.3.3) Item whose value must be a String.** This follows the IETF [Idempotency-Key draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/). The **decoded** string is validated against case-sensitive `[A-Za-z0-9._~-]{16,128}`. For example, the wire form `Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"` decodes to the key without quotes. Item parameters are ignored and never form part of the key. A parse failure, a non-String item (such as an unquoted token), or a pattern mismatch is rejected with `400`; the field is never silently ignored. Clients should send high-entropy values such as UUIDv4. Raw keys are never logged. |
| 4 | **Authorization runs before any key lookup, and the key is scoped to `(authorized organization_id, verified actor_id, 'case:create', exact idempotency_key)`.** The actor comes from a verified, server-controlled credential, and the organization from a successful `case:create` decision. The same key used by another actor in the same organization, or by the same actor in another organization, is an **independent operation**. An unauthorized or revoked actor cannot retrieve any stored idempotency result. |
| 5 | **Same scope, equal fingerprint: a replay.** No new case is created. The response is `200 OK` with `Location: /cases/{id}`, `Idempotency-Replayed: true`, and a body containing only the immutable `id` and `organization_id`, read from the idempotency record. The case's current description, status, or version is never retrieved or exposed. A replay does not confer `case:read`. |
| 6 | **Same scope, different fingerprint: a payload mismatch.** The response is `422 Unprocessable Content` with error code `idempotency_payload_mismatch`, distinct from ordinary validation errors. No case is created or modified, and no detail of the earlier request is returned. |
| 7 | **Concurrent requests in the same scope produce at most one case.** The database uniqueness constraint arbitrates. A waiting request waits for a bounded, configurable time, then returns the replay or the mismatch outcome. If the outcome is still unresolved, it returns a generic `503 Service Unavailable`; `Retry-After` may be omitted in the local demonstration. A timeout proves neither success nor failure, and the client resolves it by retrying with the same key. |
| 8 | **The idempotency record is stored atomically with case creation.** The scope, the fingerprint digest and its version, and the resulting case ID commit in the same transaction as the new case. A losing attempt leaves no extra case. |
| 9 | **Requests without a key keep distinct-creation semantics:** each successful request creates a new case. |
| 10 | **Request fingerprint, format version 1.** (1) Reject duplicate object property names *before* canonicalization (RFC 8785 requires I-JSON input). Also reject strings that are not valid Unicode, such as lone surrogates, and unknown body properties. (2) The digest is SHA-256 over the [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) (JSON Canonicalization Scheme) serialization of exactly `{"organization_id", "title", "description"}`. (3) `organization_id` is a lowercase, hyphenated UUID. `title` and `description` are the exact validated strings that will be persisted: no trimming, case folding, or Unicode normalization. (4) Store the digest and `fingerprint_version = 1`, not the full canonical payload. (5) Any change to the field set, the normalization, or the algorithm requires a new version. |
| 11 | **For the initial small, synthetic, local demonstration, idempotency records never expire.** Production deployment requires a finite retention policy, resource limits, cleanup behavior, and defined semantics after expiry. |

### Expected outcomes

| Scenario | Outcome (implemented; HTTP-tested against PostgreSQL) |
|---|---|
| Two requests without a key, identical content | Two distinct cases |
| Two requests with different keys, identical content | Two distinct cases |
| Retry by the same actor with the same key and an equivalent payload, after the first request committed | `200` replay with identity only (`id`, `organization_id`); one case in total |
| Same actor, same key, materially different payload | `422` `idempotency_payload_mismatch`; the original case is unchanged; one case in total |
| Two simultaneous requests from the same actor with the same key | Exactly one case; the later request replays, gets `422`, or gets `503` if its bounded wait expires |
| Two actors in one organization use the same key, with the same or a different payload | Independent operations; one case each; no replay, and no mismatch between them |
| The same actor uses the same key in two organizations where it is a member | Independent operations; one case for each organization |
| A requester replays its key after the case was changed by someone else | `200` replay with identity only; the changes are not disclosed |
| A replay arrives after the actor's membership was revoked | Denied before any key lookup; no stored result is revealed |
| The actor's credential is rotated, but still resolves to the same `actor.id` | The retry replays normally |
| Unquoted, non-String, or out-of-pattern `Idempotency-Key` | `400`; no case created; the request is not treated as unkeyed |
| Body with duplicate property names or unknown properties | Rejected; no fingerprint computed; no case created |
| First attempt rolled back before commit, then retried with the same key | No record was committed, so the retry creates the case once |

### Implemented transaction

The implementation ([`create_case_keyed`](../src/authorized_case_operations.py)) **reserves the key before inserting the case**, which differs from the original sketch kept below:

1. Authorize (ADR-0005): lock the actor, then the membership, `FOR SHARE`; require `case:create`. No key is read before this.
2. Reserve: `INSERT INTO case_creation_idempotency (..., case_id) VALUES (..., <new UUID>) ON CONFLICT (organization_id, actor_id, operation, idempotency_key) DO NOTHING RETURNING case_id`, with `lock_timeout` set to the bounded wait for this statement only.
   - If another transaction holds an uncommitted record for the same scope, the statement waits for it.
   - When that transaction commits, no row is returned.
   - When it rolls back, the reservation succeeds.
   - When the wait expires, `LockNotAvailable` becomes `503` and nothing was written.
3. Won the reservation: insert the case with that UUID and commit. The record's composite foreign key to `case_record (organization_id, id)` is `DEFERRABLE INITIALLY DEFERRED`, so it is checked at commit: the case and its record commit together or not at all.
4. Lost the reservation: read the committed record in the same scope, roll back, and return the replay (equal fingerprint version and digest) or `422` (anything else).

Because a losing request never inserts a case, there is no speculative case to discard and no savepoint is needed. A rollback, a terminated backend, or a failure before `COMMIT` removes the reservation with everything else, so a retry can create the case once.

**Original design sketch** (not what was implemented; kept for the record):

```sql
-- Table shape for the accepted design (implemented, with additions, in db/004):
--   case_creation_idempotency(organization_id, actor_id, operation, idempotency_key,
--                             fingerprint_version, request_fingerprint,
--                             case_id NOT NULL REFERENCES case_record, created_at)
--   UNIQUE (organization_id, actor_id, operation, idempotency_key)
-- Preconditions: credential verified; header and body validated; fingerprint (version 1) computed.
BEGIN;  -- READ COMMITTED
-- ADR-0005 write authorization: two statements, actor first, then membership.
SELECT 1 FROM actor WHERE id = $verified_actor AND is_active FOR SHARE;           -- no row -> 403
SELECT role FROM organization_membership
 WHERE actor_id = $verified_actor AND organization_id = $requested_org AND is_active
   FOR SHARE;                                    -- no row, or role lacks case:create -> 403
INSERT INTO case_record (organization_id, title, description)
VALUES ($authorized_org, $title, $description)
RETURNING id;                                         -- $new_case_id
INSERT INTO case_creation_idempotency
       (organization_id, actor_id, operation, idempotency_key,
        fingerprint_version, request_fingerprint, case_id)
VALUES ($authorized_org, $verified_actor, 'case:create', $key, 1, $fingerprint, $new_case_id)
ON CONFLICT (organization_id, actor_id, operation, idempotency_key) DO NOTHING
RETURNING case_id;
-- One row returned:  COMMIT, then 201 Created with the full creation representation.
-- No row returned:   read the committed record in the same scope (still holding the
--                    authorization locks), then ROLLBACK (discarding the new case):
--                    equal version and fingerprint -> 200 replay (id, organization_id);
--                    otherwise -> 422 idempotency_payload_mismatch.
```

Under READ COMMITTED, the second `INSERT` waits if a concurrent transaction holds an uncommitted record with the same scope. When that transaction finishes, the `INSERT` either returns no row (the other transaction committed) or proceeds (it rolled back).

The wait is bounded and configurable, for example through a lock or statement timeout; its value is set during implementation, from measurement. If the bound expires, the transaction rolls back and the response is a generic `503`. The case insert commits only when its record does, so a losing attempt leaves no case behind.

### How canonicalization is implemented and tested

Implemented as below; conformance results are in the [keyed case creation results](../tests/keyed-case-creation-results.md).

- **Parsing:** a JSON parser configured to fail on duplicate property names; Python's default `json.loads` keeps the last duplicate silently, so it needs an `object_pairs_hook` or an equivalent strict parser. The parser must also reject invalid Unicode such as lone surrogates.
- **Body validation:** reject unknown properties.
- **Header parsing:** an RFC 9651-conformant parser for the header.
- **Serialization:** an RFC 8785 serializer, from a library or a minimal implementation for this three-string object, verified against the examples in RFC 8785.
- **Tests:**
  - property-order and whitespace permutations, and escaped versus literal characters, which must give the same digest;
  - NFC versus NFD text, trailing spaces, and case differences, which must give different digests;
  - duplicate names, lone surrogates, and unknown properties, which must be rejected;
  - the header cases listed in section 5.

## 4. Remaining implementation decisions

The architecture is settled by ADR-0004. These choices are left to implementation or measurement and need no new ADR:

Each was settled during implementation:

- **Bounded wait.** A `lock_timeout` on the reservation statement only, default **2000 ms**, configurable with `EAISO_IDEMPOTENCY_WAIT_MS` (1–9000 ms).
  - **Why 2000 ms:** measured keyed creates took 121 ms at p99 end to end (median 71 ms), so the bound is about 16 times p99 and below the 5 s lock and 10 s statement timeouts.
  - **`Retry-After`:** `503` carries none.
- **Libraries.** No new third-party dependency:
  - a minimal RFC 9651 Item parser ([`src/api/structured_fields.py`](../src/api/structured_fields.py));
  - a minimal RFC 8785 serializer ([`src/request_fingerprint.py`](../src/request_fingerprint.py)).

  Both are verified against the RFC examples.
- **Storage details.** [`db/004_case_creation_idempotency.sql`](../db/004_case_creation_idempotency.sql):
  - the scope is the primary key;
  - `CHECK` constraints cover the operation vocabulary, key shape, fingerprint version, and lowercase SHA-256 hex;
  - `case_id` is `NOT NULL` and unique;
  - a deferred composite foreign key to `case_record (organization_id, id)` is backed by a new unique constraint on that pair.

These require a separate decision before they change:

- **Production retention.** A finite retention period, cleanup, resource limits, and semantics after expiry. Required before any use beyond the local demonstration.
- **Key requirement.** Whether some clients will be required to send a key.
- **Shared clients.** Whether service principals or shared clients ever need deduplication across actors. That would need a separate design for who owns an operation, and probably a superseding ADR.

## 5. Validation required before a retriable create-case API is enabled

A separate validation increment must test the implementation against a dedicated test database and record observed results. As a minimum, it must cover:

1. **Sequential replay by the same actor.** The same key and payload are submitted twice: exactly one case exists, the first response is `201`, and the second is a `200` replay containing identity only.
2. **Simultaneous requests in the same scope.** They produce exactly one case and one idempotency record.
3. **Same scope, different payload.** The second request gets `422` `idempotency_payload_mismatch`, and the first case is unchanged.
4. **Distinct keys with identical case content.** Two distinct cases are created.
5. **Two actors, one organization, same key.** With the same payload and with a changed payload, the requests are independent: two cases, no replay, no mismatch.
6. **Same actor in two organizations.** The same key used in each creates one case per organization.
7. **Requester replay after the case changed.** The replay contains only `id` and `organization_id`, with no current state.
8. **Revocation.** A replay after the membership or actor was deactivated is denied before any key lookup, and no stored result is revealed.
9. **Credential rotation.** A rotated credential that resolves to the same `actor.id` replays normally.
10. **Rollback after failure.** After a simulated failure following the case insert, neither the case nor the idempotency record persists.
11. **Ambiguous outcome.** If the bounded wait expires, the response is `503` and that request inserts nothing. A retry with the same key then resolves to a replay or a mismatch.
12. **No key.** Requests without a key create distinct cases.
13. **Header parsing.** Quoted valid keys are accepted. Unquoted, token, and integer items, bad escapes, and keys outside the pattern get `400`. Keys are case-sensitive. Parameters are ignored.
14. **Canonicalization.** The equivalence and rejection cases listed in section 3.

The same list appears in [ADR-0004](adr/0004-keyed-idempotent-case-creation.md#revisit-when). All of them were executed against PostgreSQL before keyed creation was enabled; observed results, including real fault injection and negative controls, are in the [keyed case creation results](../tests/keyed-case-creation-results.md).

Keyed creation is duplicate-safe only for retries that reuse the same key, actor, organization, and payload. It does not provide exactly-once network delivery, and requests without a key remain non-idempotent.

## 6. Related documents

- [ADR-0004: Keyed Idempotent Case Creation](adr/0004-keyed-idempotent-case-creation.md): the accepted decision and its tradeoffs; [implementation results](../tests/keyed-case-creation-results.md).
- [System context](system-context.md): current components, capability status, and trust boundaries.
- [Controlled case operations](case-operations.md): the local case service and its limits.
- [Data model](data-model.md): `case_record` attributes and integrity rules.
- [Requirements](requirements.md): the "Retries" rule and the "Correctness" quality requirement.
