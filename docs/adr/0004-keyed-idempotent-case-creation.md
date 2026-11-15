# ADR-0004: Keyed Idempotent Case Creation

- **Status:** Proposed
- **Recorded:** 2026-11-13
- **Decision provenance:** Contemporary proposal. It formalizes the design direction first documented in [Case creation semantics, section 3](../case-creation-semantics.md#3-design-decision-distinct-creation-by-default-idempotent-creation-by-key) on 2026-11-13.
  - **Revised while Proposed (2026-11-15):** the key scope changed from `(organization, operation, key)` to `(authorized organization, verified actor, operation, key)`, and the replay, conflict, in-flight, key-format, fingerprint, and retention rules were proposed. The reason is the cross-actor replay risk identified in the [request sequence](../authentication-authorization-sequence.md#9-requester-create-only-and-idempotent-replay).
  - This ADR was never Accepted, so it is revised in place rather than superseded. **Idempotent creation is not implemented, and no idempotency tests exist.**
- **Legacy reference:** [Case creation semantics](../case-creation-semantics.md), section 3
- **Related requirements:** FR-01, FR-02, FR-11; rules "Organization ownership", "Retries", and "Concurrency"; quality requirements "Security" and "Correctness" ([requirements](../requirements.md))
- **Implementation evidence:** None for idempotent creation.
  - [`src/case_service.py`](../../src/case_service.py): `create_case()` inserts a new case on every successful call.
  - [`db/001_init.sql`](../../db/001_init.sql): defines no uniqueness constraint on `case_record` beyond its primary key.
  - No credential resolver, authorizing API, or keyed-creation test exists.

## Problem

A `POST /cases` request can commit even though the client never receives the response. A blind retry must not create a second case for the same intended operation. Two genuine incidents can have identical text, however, so case content cannot identify a retry.

There is also a security constraint. When two actors share an organization, neither may retrieve, replay, or probe the other's create operation by reusing an idempotency key. This matters most for `requester`, which is proposed to hold `case:create` but not `case:read` ([permission policy](../authorization-permissions.md)).

## Constraints

- **PostgreSQL is authoritative** ([ADR-0001](0001-postgresql-authoritative-case-store.md)). The case and its idempotency record must commit atomically, and a database uniqueness constraint must arbitrate concurrent requests.
- **No identity yet.** Identity and authorization are not implemented. The planned design resolves a **server-verified `actor.id`**, checks an active membership, and checks a per-action permission ([ADR-0005](0005-server-verified-identity-and-tenant-authorization.md), Proposed). An actor ID never comes from a request body, URL, or header.
- **Content is not identity.** Legitimate cases may share content, so title and description cannot be unique. The seed script's title-based duplicate check is setup behavior, not evidence of idempotency.
- **No API yet.** No HTTP API exists. This ADR describes future behavior, not a working guarantee.

## Options Considered

1. **Key scoped to the actor and the organization (preferred).** The unique scope is `(organization_id, actor_id, operation, key)`, with the organization and actor taken from a successful authorization decision.
   - *Benefits:* no actor can replay or probe another actor's key. An actor's own retries stay stable across credential rotation, as long as the credential resolves to the same `actor.id`.
   - *Costs:* two actors who submit one logical incident are **not** deduplicated. Each index entry is larger. This does not depend on how hard keys are to guess.
2. **Key scoped to the organization only (the earlier proposal in this ADR, now replaced).** The unique scope is `(organization_id, operation, key)`.
   - *Benefits:* simpler, and it suits a single shared client.
   - *Costs:* within one organization, a create-only actor who reuses or guesses another member's key could replay that member's case, or learn from a conflict that the key exists.
3. **Deduplicate by content, or always insert.** Content matching discards legitimate identical incidents. Always inserting leaves ambiguous retries unprotected. Neither meets the requirements.

## Decision

**Proposed: adopt option 1**, subject to review by the architecture owner. The detailed rules, outcome table, and validation list are in [Case creation semantics](../case-creation-semantics.md#3-design-decision-distinct-creation-by-default-idempotent-creation-by-key).

1. **Authorization before key lookup.** On every keyed request, including replays, authorize first:
   1. authenticate;
   2. resolve the stable `actor.id`;
   3. require an active actor, an active membership in the selected organization, and `case:create`.

   Only then look up or reserve the key. A denied request gets the ordinary `401` or `403`, and the response reveals nothing about stored keys.
2. **Scope.** The unique identity of a keyed create is **`(authorized organization_id, verified actor_id, 'case:create', exact key)`**. The same literal key used by another actor, or in another organization, is an **independent operation**: it can neither replay nor conflict with the other record. Neither an organization UUID nor a key confers permission.
3. **Key format.** `Idempotency-Key` stays optional.
   - When present, it must match the case-sensitive pattern `[A-Za-z0-9._~-]{16,128}`; otherwise the request is rejected with `400`.
   - Clients should send high-entropy values, such as UUIDv4. Unpredictability is **not** the access control.
   - Raw keys are not logged.
4. **Request fingerprint.**
   - **Validation first:** the request is validated strictly, and unknown body fields are rejected.
   - **Fingerprint:** SHA-256 over the [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) (JSON Canonicalization Scheme) serialization of `{"organization_id", "title", "description"}`.
   - **Values:** `organization_id` is in lowercase hyphenated UUID form. `title` and `description` are taken exactly as parsed and validated, which is the text that will be persisted. There is no trimming, case folding, Unicode normalization, or whitespace change.
   - **What does not matter:** JSON key order, insignificant whitespace, and escape spelling, because the fingerprint uses parsed values.
   - **Redundancy:** `organization_id` also appears in the scope; repeating it is harmless. The actor and operation appear only in the scope.
   - **Future changes:** any later input normalization is a contract change.
5. **New create.** One PostgreSQL transaction commits exactly one case and its idempotency record, under a unique constraint on the full scope, and the response is `201 Created` with the full creation representation. A rollback removes both; no record ever exists without its case.
6. **Replay.** A request whose scope and fingerprint match an existing record gets **`200 OK`** with:
   - `Location: /cases/{id}`;
   - `Idempotency-Replayed: true`;
   - a minimal body taken from the idempotency record, containing only immutable identity: `id` and `organization_id`.

   The replay never reads or returns the current status, text, or version of the case. The projection is the same for every role. The `201` and `200` bodies differ by design. For a create-only requester, the replay is **not** a `case:read` grant.
7. **Conflict.** The same scope with a different fingerprint gets **`409 Conflict`**, with no new case, no mutation, and no details of the first request. Because the scope includes the actor, a conflict can only reveal the caller's own keys.
8. **In-flight requests.** The unique constraint and the transaction serialize concurrent requests in the same scope:
   - a second request waits, with a bounded timeout (value not chosen), and then returns the replay or conflict outcome;
   - if the outcome still cannot be established in time, it returns a generic **`503`** that is safe to retry with the same key. That response claims neither success nor rollback, and contains no case data.

   A response lost after commit leaves the outcome **unknown** until a retry with the same key resolves it.
9. **No key.** Each successful request without a key creates a distinct case, even with identical content. Distinct keys also create distinct cases.
10. **Retention for the local demonstration.** Committed idempotency records are **never expired or deleted**, so a key cannot be reused within its scope. This is a deliberate, bounded tradeoff for synthetic local data, **not** a production retention policy.

**Status-code divergence for review.** The IETF [Idempotency-Key header field draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) is an Internet-Draft, not a standard, and its recommendations differ from points 7 and 8:

| Situation | IETF draft | This proposal | Reason for this proposal |
|---|---|---|---|
| Key reused with a different payload | `422` | `409` | `422` is already this contract's code for invalid business fields |
| Retry while the original request is still running | Immediate `409` | Wait, then replay or conflict; `503` if the wait times out | Clients get a definite answer whenever possible |

The architecture owner must confirm or change these before acceptance.

## Risks and Consequences

- **No cross-actor deduplication.** Two actors who submit one incident with the same key create two cases. If shared clients or service principals ever need cross-actor deduplication, that needs a separately authenticated design for who owns an operation, not a weaker scope.
- **Credential continuity.** Retries work across credential rotation only if the credential still resolves to the same `actor.id`. Re-identifying an actor changes its key scope.
- **Different response projections.** A replay returns less than the original create. Clients must not treat a `200` replay as a read of the current state.
- **Unbounded retention.** Records accumulate without expiry. Any deployment beyond the local demonstration needs finite retention, defined behavior after expiry, and limits on resource use.
- **Timeouts and ambiguity.** A `503` or a lost response does not reveal the outcome. Clients must retry with the same key, and tests must not infer an outcome from a timeout.
- **Rolled-back case inserts.** In the illustrative transaction, a request that loses a race inserts a case and then rolls it back. That is harmless, but it is wasted work.
- **No evidence.** No idempotency or isolation claim is warranted until PostgreSQL-backed and HTTP tests have actually passed.

## Revisit When

- **Before Accepted.** The following prerequisites concern the design. Executed tests are **not** among them; Accepted means an adopted design, not an implementation.
  - The architecture owner approves the rules above, including the status-code divergence and the exact fingerprint serialization.
  - Compatibility with [ADR-0005](0005-server-verified-identity-and-tenant-authorization.md) is confirmed.
  - The [case creation semantics](../case-creation-semantics.md), [API contract](../api-contract.md), [permission policy](../authorization-permissions.md), and [request sequence](../authentication-authorization-sequence.md) stay aligned with these rules.
- **Before keyed creation is enabled.** Tests against the dedicated test database must be implemented and executed, with the observed results recorded separately. They must cover:
  1. sequential and concurrent replay by the same actor;
  2. a conflict on a changed payload;
  3. different keys with identical content;
  4. the same key used by two actors in one organization, with the same payload and with a changed payload;
  5. the same actor in two organizations;
  6. organization isolation;
  7. a replay after the membership was revoked;
  8. credential rotation that preserves the actor;
  9. a requester replay after the case changed;
  10. rollback;
  11. timeout and uncertain-outcome handling;
  12. requests without a key.
- **Before production use.** Design finite retention and expiry, resource limits, credential security, least-privilege database access, and recovery procedures. Revisit if service principals or shared clients must retry on behalf of several actors.
