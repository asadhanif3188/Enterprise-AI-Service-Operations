# ADR-0004: Keyed Idempotent Case Creation

- **Status:** Accepted (2026-11-15). The architecture owner approved the **design**. At acceptance, keyed creation was not built and no idempotency test existed. It was implemented for the local HTTP API on 2026-11-16 (see Implementation evidence). The decision text below is unchanged; its statements about missing implementation describe the state at acceptance.
- **Recorded:** 2026-11-13
- **Decision provenance:** Contemporary proposal. It formalizes the design direction first documented in [Case creation semantics, section 3](../case-creation-semantics.md#3-design-decision-distinct-creation-by-default-idempotent-creation-by-key) on 2026-11-13.
  - **Revised while Proposed (2026-11-15):** the key scope changed from `(organization, operation, key)` to `(authorized organization, verified actor, operation, key)`, because of the cross-actor replay risk identified in the [request sequence](../authentication-authorization-sequence.md#9-requester-create-only-and-idempotent-replay). The replay, in-flight, fingerprint, and retention rules were proposed at the same time.
  - **Finalized at acceptance (2026-11-15):**
    - a changed-payload reuse now returns `422` (it was proposed as `409`);
    - the header is parsed as an RFC 9651 Structured Field String;
    - duplicate JSON property names are rejected;
    - the fingerprint is stored with a format version.
- **Legacy reference:** [Case creation semantics](../case-creation-semantics.md), section 3
- **Related requirements:** FR-01, FR-02, FR-11; rules "Organization ownership", "Retries", and "Concurrency"; quality requirements "Security" and "Correctness" ([requirements](../requirements.md))
- **Implementation evidence:** None at acceptance (2026-11-15). Implemented on 2026-11-16 for the local demonstration; current status is kept in living documents ([case creation semantics](../case-creation-semantics.md), [system context](../system-context.md#4-capability-status)).
  - [`db/004_case_creation_idempotency.sql`](../../db/004_case_creation_idempotency.sql): the idempotency table, with the full scope as its primary key and a deferred composite foreign key to `case_record (organization_id, id)`.
  - [`src/authorized_case_operations.py`](../../src/authorized_case_operations.py) (`create_case_keyed`), [`src/request_fingerprint.py`](../../src/request_fingerprint.py), and [`src/api/structured_fields.py`](../../src/api/structured_fields.py): authorization before key reservation, the RFC 8785 fingerprint version 1, and RFC 9651 header parsing, exposed through `POST /cases` in [`src/api/`](../../src/api/). Identity and authorization come from the ADR-0005 implementation.
  - [Keyed case creation results](../../tests/keyed-case-creation-results.md): the tests listed under Revisit When, executed against PostgreSQL 16, including real concurrency and lost-commit faults and negative controls. This is local, synthetic evidence, not production assurance.
  - Unchanged: [`src/case_service.py`](../../src/case_service.py) `create_case()` and requests without a key remain **non-idempotent**, and idempotency records never expire.

## Problem

A `POST /cases` request can commit even though the client never receives the response. A blind retry must not create a second case for the same intended operation. Two genuine incidents can have identical text, however, so case content cannot identify a retry.

There is also a security constraint. When two actors share an organization, neither may retrieve, replay, or probe the other's create operation by reusing an idempotency key. This matters most for `requester`, which is proposed to hold `case:create` but not `case:read` ([permission policy](../authorization-permissions.md)).

## Constraints

- **PostgreSQL is authoritative** ([ADR-0001](0001-postgresql-authoritative-case-store.md)). The case and its idempotency record must commit atomically, and a database uniqueness constraint must arbitrate concurrent requests.
- **Identity comes from ADR-0005.** ADR-0005 was Proposed when this ADR was accepted; it was itself Accepted later on 2026-11-15. This decision depends on [ADR-0005](0005-server-verified-identity-and-tenant-authorization.md) for the verified `actor.id`, the organization membership, and the authorization decision. Neither is implemented. An actor ID never comes from a request body, URL, or header.
- **Content is not identity.** Legitimate cases may share content, so title and description cannot be unique. The seed script's title-based duplicate check is setup behavior, not evidence of idempotency.
- **Header standard.** The IETF [Idempotency-Key HTTP header field draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) (revision -08, an Internet-Draft, not a standard) defines the header as an Item Structured Header Field whose value is a String ([RFC 9651 §3.3.3](https://www.rfc-editor.org/rfc/rfc9651#section-3.3.3)).

## Options Considered

1. **Key scoped to the actor and the organization (chosen).** The unique scope is `(organization_id, actor_id, operation, key)`, with the organization and actor taken from a successful authorization decision.
   - *Benefits:* no actor can replay or probe another actor's key. An actor's own retries survive credential rotation, as long as the credential resolves to the same `actor.id`.
   - *Costs:* two actors who submit one logical incident are not deduplicated, and each index entry is larger.
2. **Key scoped to the organization only (the earlier proposal, rejected).**
   - *Benefits:* simpler, and suits a single shared client.
   - *Costs:* within one organization, a create-only actor who reuses or guesses another member's key could replay that member's case, or learn that the key exists.
3. **Deduplicate by content, or always insert (rejected).** Content matching discards legitimate identical incidents. Always inserting leaves ambiguous retries unprotected.

## Decision

**Adopt option 1.** The detailed rules, outcome table, and validation list are in [Case creation semantics](../case-creation-semantics.md#3-design-decision-distinct-creation-by-default-idempotent-creation-by-key).

1. **Authorization before key lookup.** On every keyed request, including replays, authorize first:
   1. authenticate;
   2. resolve `actor_id` from a verified, server-controlled credential;
   3. require an active actor, an active membership in the selected organization, and `case:create`.

   Only then look up or reserve the key. An unauthorized or revoked actor gets the ordinary `401` or `403` and **cannot retrieve any stored idempotency result**.
2. **Scope.** The unique identity of a keyed create is **`(authorized organization_id, verified actor_id, 'case:create', exact idempotency_key)`**. These are independent operations that can neither replay nor conflict with each other:
   - two actors using the same key in one organization;
   - one actor using the same key in two organizations.

   Neither an organization UUID nor a key confers permission.
3. **`Idempotency-Key` header.** The header is optional, and requests without it keep distinct creation.
   - **Parsing:** parse the field as an RFC 9651 Item, which must be a **String**. The quoted wire form `Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"` decodes to the key `8e03978e-40d5-43e8-bc93-6894a57f9324`.
   - **Key validation:** validate the **decoded** value against the case-sensitive pattern `[A-Za-z0-9._~-]{16,128}`.
   - **Parameters:** ignore Item parameters, as RFC 9651 §2.3 discourages treating unrecognized parameters as errors. Parameters never form part of the key.
   - **Malformed headers:** a parse failure, a non-String item (for example an unquoted token), or a pattern mismatch is rejected with `400`. RFC 9651 §4.2 allows either ignoring an unparseable field or treating the whole message as malformed. This design chooses **malformed**, because ignoring the field would silently turn a retry into a distinct creation.
   - **Keys and logging:** clients should send high-entropy values such as UUIDv4. Unpredictability is not access control. Raw keys are never logged.
4. **Request fingerprint, format version 1.**
   - **Strict JSON parsing.** Reject duplicate object property names *before* canonicalization; RFC 8785 requires I-JSON input. Reject strings that are not valid Unicode, such as lone surrogates, and reject unknown body properties.
   - **Digest.** SHA-256 over the [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) (JSON Canonicalization Scheme) serialization of exactly `{"organization_id", "title", "description"}`.
   - **Values.** `organization_id` is a lowercase, hyphenated UUID. `title` and `description` are the exact validated strings that will be persisted: no trimming, case folding, or Unicode normalization.
   - **Storage.** Store the digest and `fingerprint_version = 1`, not the full canonical payload.
   - **Changes.** Any change to the field set, the normalization, or the algorithm requires a new version.
5. **New create.** One PostgreSQL transaction commits exactly one case and its idempotency record, under a unique constraint on the full scope. The response is `201 Created` with the full creation representation. A rollback removes both.
6. **Replay.** The same scope with an equal fingerprint gets **`200 OK`** with:
   - `Location: /cases/{id}`;
   - `Idempotency-Replayed: true`;
   - a body containing only the immutable `id` and `organization_id`, read from the idempotency record.

   The case's current description, status, or version is never retrieved or exposed, and the projection is the same for every role. A replay does **not** confer `case:read`.
7. **Payload mismatch.** The same scope with a different fingerprint gets **`422 Unprocessable Content`** with error code `idempotency_payload_mismatch`. This is distinct from ordinary validation errors. The response has no new case, no mutation, and no details of the earlier request. Because the scope includes the actor, the response can only reveal the caller's own keys.
8. **In-flight requests.**
   - **Arbitration:** the unique constraint and the transaction arbitrate between concurrent requests in the same scope; at most one case commits, and a losing attempt leaves no extra case.
   - **Bounded wait:** a waiting request waits for a bounded, **configurable** time. The value is set during implementation, from measurement. It then returns the replay or the mismatch outcome.
   - **Timeout:** if the outcome is still unresolved, it returns a generic **`503 Service Unavailable`**. `Retry-After` may be omitted in the local demonstration.
   - **Unknown outcomes:** a timeout or a lost response proves neither success nor failure. The client resolves the outcome by retrying with the same key.
9. **No key.** Each successful request without a key creates a distinct case.
10. **Retention.** For the initial small, synthetic, local demonstration, idempotency records **never expire**. Production deployment requires a finite retention policy, resource limits, cleanup behavior, and defined semantics after expiry (see Revisit When).

**Relationship to the IETF draft:** one row is aligned and two are deliberate deviations.

| Situation | Draft -08 | This decision | Reason |
|---|---|---|---|
| Key reused with a different payload | `422` | `422`, with code `idempotency_payload_mismatch` | Aligned with the draft. The specific code separates this case from ordinary validation errors. |
| Retry while the original request is still in progress | Immediate `409` | A bounded wait, then replay or mismatch; `503` if unresolved | Gives clients a definite answer whenever possible. `503` signals that the outcome is unknown and that retrying with the same key is safe. |
| Unparseable header | The draft defers to RFC 9651, which allows either ignoring the field or treating the message as malformed | Malformed: `400` | Ignoring the field would turn an intended retry into a distinct creation. |

**Dependency on ADR-0005.** This acceptance covers the idempotency design only. Authentication and authorization are not implemented, and keyed creation stays **disabled** until three conditions hold:

- ADR-0005 is Accepted (satisfied on 2026-11-15);
- both ADRs are implemented;
- the tests below have passed and their results are recorded.

## Risks and Consequences

- **No cross-actor deduplication.** Two actors who submit one incident with the same key create two cases. Shared clients or service principals would need a separately authenticated design for who owns an operation.
- **Credential continuity.** Retries survive credential rotation only when the credential still resolves to the same `actor.id`.
- **Different response projections.** A `200` replay carries less than a `201` creation. Clients must not treat a replay as a read of the current state.
- **Strict headers.** Clients that send unquoted keys get `400`. Header parsing must use a correct RFC 9651 implementation.
- **Canonicalization errors.** A serializer that deviates from RFC 8785, or a parser that accepts duplicate property names, would misclassify retries. This must be tested against RFC 8785 examples and adversarial inputs.
- **Unbounded retention.** Records accumulate without expiry. This is acceptable only for the local demonstration.
- **Timeouts and ambiguity.** A `503` or a lost response does not reveal the outcome. Clients must retry with the same key.
- **Not secured yet.** Until ADR-0005 (Accepted 2026-11-15) is implemented, no idempotency or tenant-isolation behavior exists or may be claimed.

## Revisit When

**Accepted design decisions (change only through a superseding ADR):**

- the scope, and authorization before key lookup;
- the RFC 9651 String header syntax with key validation and rejection of malformed headers;
- fingerprint version 1 and digest-only storage;
- the response rules: `201` creation; `200` identity-only replay; `422` `idempotency_payload_mismatch`; bounded wait then `503`; distinct creation without a key;
- atomic creation under database uniqueness;
- no expiry in the local demonstration.

**Implementation choices, configurable or measured later (no new ADR needed):**

- the bounded-wait mechanism and its value (for example a lock or statement timeout);
- whether `503` carries `Retry-After`;
- the RFC 9651 parser and RFC 8785 serializer libraries, or a minimal verified serializer;
- column types and indexes;
- error-message wording.

**Tests required before keyed creation is enabled** (dedicated test database; observed results recorded separately):

1. Sequential and concurrent replay by the same actor; exactly one case.
2. A changed payload gets `422` `idempotency_payload_mismatch`, and the first case is unchanged.
3. Different keys with identical content; requests without a key.
4. Two actors with the same key in one organization, with the same and with a changed payload; the same actor with the same key in two organizations.
5. A replay after the actor or membership was revoked gets a denial and reveals no stored result.
6. Credential rotation that preserves the `actor.id`.
7. A requester replay after the case changed returns only `id` and `organization_id`.
8. Rollback, and timeout or uncertain-outcome handling.
9. Header parsing:
   - quoted valid keys;
   - unquoted, token, or integer items;
   - bad escapes;
   - keys outside the pattern;
   - case sensitivity;
   - ignored parameters.
10. Canonicalization:
    - RFC 8785 examples;
    - property-order and whitespace permutations;
    - escaped versus literal characters;
    - NFC versus NFD and trailing spaces, which must stay distinct;
    - duplicate property names, lone surrogates, and unknown properties, which must be rejected.

**Production-readiness requirements:**

- finite retention, expiry semantics, cleanup, and resource limits;
- production credential security;
- least-privilege database access;
- operational recovery.

Revisit if service principals or shared clients must retry on behalf of several actors, if the IETF draft changes materially, or if ADR-0005 is rejected or substantially changed.
