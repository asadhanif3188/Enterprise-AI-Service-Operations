# Engineering Findings Register

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** Living document. Findings are added when they are discovered and closed only with evidence.  
**Owner:** Architecture owner of this repository.

## 1. Purpose and scope

This register records technical findings discovered during:

- architecture reviews;
- code reviews;
- controlled experiments;
- validation runs.

Each entry tracks a finding from identification to closure: what was observed, why it matters, what would resolve it, and what evidence closed it.

**In scope:** behavior that differs from an accepted decision, a contract, or a requirement. This includes correctness, security, reliability, performance, and maintainability concerns in the code, schema, tests, or documentation.

**Out of scope:**

- **Planned features.** These are tracked as next increments in the living design documents, such as the [API contract](api-contract.md#12-next-implementation-increments).
- **Architectural decisions.** These belong in [ADRs](adr/README.md). A finding may *lead* to an ADR or an ADR revision. It does not replace one, and most findings need none.
- **Test reports.** Reports in [`tests/`](../tests/) are historical evidence and are not edited to reflect findings.

## 2. Identifiers

Identifiers have the form `FDE-C<n>-<AREA>-<NNN>`:

- `FDE` is a fixed register prefix.
- `C<n>` is the review cycle in which the finding was raised.
- `<AREA>` is a short component code, for example `READ` for single-case retrieval.
- `<NNN>` is a three-digit sequence within that cycle and area.

**Rules:**

- An identifier is assigned once and is **never reused**, even after closure or withdrawal.
- A finding that is split or superseded keeps its identifier and links to the new entries.

## 3. Classification

**Category:** `Security`, `Correctness`, `Reliability`, `Performance`, `Maintainability`, or `Documentation`. Choose the primary concern, and describe secondary impacts in the record.

**Severity** reflects impact if the finding is left unresolved in the system's current scope (a local, single-node demonstration with synthetic data):

| Severity | Meaning |
|---|---|
| Critical | Confirmed unauthorized data access or mutation across a trust or tenant boundary, or data loss. Work stops until it is contained. |
| High | A plausible path to unauthorized access, a broken invariant on authoritative data, or a fail-open dependency path. |
| Medium | Externally visible contract violation in a common path, or a reliability gap without data exposure. |
| Low | Contract deviation limited to an uncommon condition, with no observed data exposure and fail-closed behavior preserved. |
| Informational | An observation worth recording that needs no change by itself. |

**Status:**

| Status | Meaning |
|---|---|
| `OPEN` | Recorded and not yet resolved. One qualifier is added: <br>— *untriaged*: severity or ownership is not yet agreed;<br>— *accepted limitation*: acknowledged and tolerated in the current scope, pending a deliberate decision;<br>— *in progress*: a fix or investigation is under way. |
| `RESOLVED — pending verification` | A change or decision exists, but the closure evidence required by [section 7](#7-closure-requirements) is not yet recorded. |
| `CLOSED — fixed` | Resolved and verified with recorded evidence. |
| `CLOSED — risk accepted` | Deliberately not changed, with the remaining risk stated and accepted by the owner. |
| `CLOSED — withdrawn` | Not reproducible or not a finding; the reason is recorded. |

## 4. Summary

| ID | Title | Category | Severity | Status | Component | Target milestone |
|---|---|---|---|---|---|---|
| [FDE-C1-READ-001](#fde-c1-read-001) | Inconsistent inactive-actor HTTP status under concurrent revocation | Correctness | Low | OPEN — accepted limitation | `GET /cases/{id}` | Transaction and concurrency correctness review |
| [FDE-C1-IDEM-001](#fde-c1-idem-001) | Unrelated organization-row lock misclassified as in-flight idempotent request | Correctness | Low | OPEN — accepted limitation | `POST /cases` with `Idempotency-Key` | Transaction and concurrency correctness review |
| [FDE-C1-DOC-001](#fde-c1-doc-001) | V22 route-absence evidence attributed as a database-backed state assertion | Documentation | Informational | CLOSED — fixed | `docs/authorization-permissions.md`, section 6 | Authorization evidence accuracy review |

## 5. Finding records

### FDE-C1-READ-001

**Inconsistent inactive-actor HTTP status under concurrent revocation**

| Field | Value |
|---|---|
| Category | Correctness (API response classification) |
| Severity | Low |
| Status | OPEN — accepted limitation |
| Discovered | 2026-11-16, during a review of authenticated case retrieval and scoped listing |
| Affected component | `GET /cases/{id}` |
| Affected implementation | [`src/authorized_case_operations.py`](../src/authorized_case_operations.py), method `get_case()` |
| Related decision | [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md): the failure-status table (inactive actor → `403`) and the minimum revocation contract |
| Target milestone | Transaction and concurrency correctness review |
| Current disposition | Deferred for deliberate architectural review. Not a blocker for the already accepted local read/list demonstration. |

**Observed behavior.**

- In a controlled local interleaving, an actor was deactivated, and the change committed, *after* `get_case()`'s preceding actor-status query had observed the actor as active, and *before* the authorized case-read query ran.
- The case-read query observed the inactive actor and returned no row.
- The operation therefore raised `CaseNotFound`. The HTTP error mapping turns that into the generic `404 case_not_found`, not the `403 actor_inactive` that ADR-0005 assigns to an inactive actor.

**Expected behavior.** ADR-0005 specifies `403` for a verified credential whose actor is inactive.

ADR-0005 also accepts that each read statement runs under its own snapshot. It does not define which classification applies when the actor's state changes *between* the statements of one request. This is why acceptance criterion 2 below asks for that definition.

**Root cause.**

- `get_case()` runs the actor-status check and the authorized case-read query as **two separate SQL statements** on one autocommit connection, under PostgreSQL READ COMMITTED.
- Each statement takes its own snapshot, so the two can observe different committed states.
- The case-read query repeats the active-actor predicate, as ADR-0005 requires. That predicate correctly denies access, but the denial surfaces through the "no row" path, which is reported as `404`.
- By contrast, `list_cases()` evaluates actor status, authorization, and rows in one statement, and so under one snapshot.

**Security assessment.**

- No unauthorized case data was observed. The later query rejected access, and the response was the generic, nondisclosing `404`.
- The deviation concerns the classification of a denied response under one specific concurrent interleaving. It is **not** a confirmed cross-tenant vulnerability.
- This assessment is limited to the interleaving that was examined. It is not a comprehensive security verification. It does not prove that no information side channel exists: for example, timing differences are not addressed, as ADR-0005 already notes.

**Correctness impact.** Within a narrow concurrency window, the externally visible status for an inactive actor can be `404` instead of the specified `403`. A client relying on `403 actor_inactive` to detect deactivation could misread the response as a missing case.

**Reproduction and evidence provenance.**

- **How it was observed.** The behavior was observed in an isolated, disposable local PostgreSQL 16.14 environment containing only synthetic data. The authorized-case-operations layer was called directly, and the actor deactivation was committed at the exact point between the two statements.
- **How the HTTP status was derived.** The `404` follows from the fixed mapping of `CaseNotFound` in [`src/api/errors.py`](../src/api/errors.py). In the same review, real HTTP requests against a running local server confirmed that mapping and the other read/list outcomes, but **they did not exercise this interleaving**.
- **What is not public.** The working notes and raw scratch output from that run are not published and are not evidence in this repository.
- **No committed regression test reproduces this race.** The existing authorization tests cover related behavior but **not** this interleaving:
  - [`tests/test_case_authorization.py`](../tests/test_case_authorization.py) shows, for example, that reads do not wait for an uncommitted revocation and are denied after it commits;
  - [`tests/test_http_case_api.py`](../tests/test_http_case_api.py) maps the inactive-actor and not-found outcomes over HTTP.
- **Related design discussion.** [Authentication and authorization sequence, section 8](authentication-authorization-sequence.md#8-checkuse-timing-and-revocation).

**Decision and options.** Not established. The options to evaluate, without predetermining the outcome:

1. **One statement, one snapshot.** Perform actor-status classification and authorized case retrieval in one SQL statement, as `list_cases()` already does. The statement must still distinguish the inactive actor from a nondisclosing `404`.
2. **Keep the current implementation.** Explicitly document the weaker guarantee: inactive-actor classification is best-effort at a statement-snapshot boundary, while denial itself is guaranteed.

The outcome determines whether documentation, an ADR revision, or a superseding ADR is required (criterion 8).

**Acceptance criteria.**

1. Reproduce the exact concurrent revocation interleaving.
2. Define the intended classification semantics at a PostgreSQL statement-snapshot boundary.
3. Evaluate the design alternatives and record the decision.
4. Preserve nondisclosing `404` behavior for missing, foreign, and unreadable cases.
5. Preserve tenant isolation and fail-closed dependency behavior.
6. Add focused automated regression and concurrency tests if a correction is implemented.
7. Execute the relevant regression suite and record exact results.
8. Update documentation or the ADR lifecycle according to the architectural impact.
9. Close the finding only with verifiable evidence, or explicitly accept the remaining risk.

**Resolution summary:** Not established.  
**Verification evidence:** Not established.  
**Closure date:** Not established.

### FDE-C1-IDEM-001

**Unrelated organization-row lock misclassified as in-flight idempotent request**

| Field | Value |
|---|---|
| Category | Correctness (API response classification); secondary operability impact (diagnostics and log attribution) |
| Severity | Low |
| Status | OPEN — accepted limitation |
| Discovered | 2026-11-16, during independent sequential and concurrent idempotency validation |
| Affected component | `POST /cases` with an `Idempotency-Key` header |
| Affected implementation | [`src/authorized_case_operations.py`](../src/authorized_case_operations.py), method `create_case_keyed()`, reservation phase (`phase = "reserve"`) |
| Related decision | [ADR-0004](adr/0004-keyed-idempotent-case-creation.md#decision): decision item 8, in-flight requests (bounded wait, then `503`) |
| Target milestone | Transaction and concurrency correctness review |
| Current disposition | Deferred for deliberate review. The operation fails closed and retry guidance remains safe. |

**Observed behavior.**

- In a disposable PostgreSQL 16.14 environment with synthetic data, a separate session held the organization row `FOR UPDATE`.
- A keyed create for that organization then encountered a foreign-key-related lock wait during its idempotency reservation.
- After approximately two seconds, it returned `503 idempotency_request_in_flight`.
- **No competing request held the same idempotency key.**
- The operation failed closed: it created no case and no idempotency record.
- After the unrelated organization-row lock was released, the same keyed request succeeded.

**Expected behavior.** ADR-0004 assigns `503` to the case where a **competing request in the same scope** is still unresolved after the bounded wait. A lock wait with any other cause is not that condition. The intended HTTP classification for non-idempotency lock waits is not yet defined; acceptance criterion 2 asks for that definition.

**Root cause (from source review; inferred, not separately executed).**

- `create_case_keyed()` lowers `lock_timeout` to the idempotency wait (default 2000 ms) for the reservation `INSERT INTO case_creation_idempotency ... ON CONFLICT DO NOTHING`.
- Any `LockNotAvailable` raised while `phase == "reserve"` is classified as an in-flight idempotent request, raised as `IdempotencyRequestInFlight`, and mapped to `503 idempotency_request_in_flight` in [`src/api/errors.py`](../src/api/errors.py). It is also logged as `authz.idempotency_in_flight`.
- The reservation `INSERT` also checks the record's foreign keys, which lock the referenced organization and actor rows. A source comment at that statement states that these foreign-key locks "do not conflict with ordinary activity". A `FOR UPDATE` lock on the organization row does conflict with them, so a wait on that lock is classified the same way as a wait on a same-scope reservation.
- The classification therefore uses the statement in which the wait occurred, not the lock that was waited on.

**Reproduction and evidence provenance.**

- **How it was observed.** The scenario was reported from **one** in-process ASGI client test against real PostgreSQL 16.14, with database blocking evidence showing the keyed request waiting behind the session that held the organization row.
- **What was reviewed separately.** The exception classification described under *Root cause* was independently reviewed in source code. That review is reasoning from code, not an additional execution.
- **What is not public.** The private validation closeout and the raw scratch output from that run are not published and are not evidence in this repository.
- **No committed regression test reproduces this scenario.** The existing keyed-creation tests in [`tests/test_keyed_case_creation.py`](../tests/test_keyed_case_creation.py) cover related behavior but **not** an unrelated organization-row lock:
  - `test_bounded_wait_returns_503_then_retry_resolves` produces the `503` with a competing request that holds the **same** key;
  - `test_unrelated_key_proceeds_while_a_reservation_is_held` shows that a **different** key is not blocked by a held reservation; no organization-row lock is involved.
- **Historical report.** The [keyed case creation results](../tests/keyed-case-creation-results.md) predate this finding and are not edited to reflect it.

**Impact.**

- **Correctness and diagnostics.** The response inaccurately attributes a general database lock wait to a concurrent same-scope idempotent request. The error code and the `authz.idempotency_in_flight` log event both name the wrong cause, which can mislead clients and operators.
- **Limits.** Retry guidance remains safe: nothing was written, and retrying with the same key resolves the request once the lock is released. No duplicate case, partial write, or data exposure was observed.

**Decision and options.** Not established. Options are to be evaluated under acceptance criterion 3, including their locking and deadlock implications.

**Acceptance criteria.**

1. Add a deterministic committed regression test for the unrelated organization-lock scenario.
2. Define the intended HTTP classification for non-idempotency lock waits.
3. Evaluate correction options and their locking and deadlock implications.
4. Preserve atomic creation, authorization before key lookup, and existing replay semantics.
5. Run the relevant regression suites and record exact results.
6. Close only after verification, or after an explicit, documented risk-acceptance decision.

**Resolution summary:** Not established.  
**Verification evidence:** Not established.  
**Closure date:** Not established.

### FDE-C1-DOC-001

**V22 route-absence evidence attributed as a database-backed state assertion**

| Field | Value |
|---|---|
| Category | Documentation (evidence accuracy) |
| Severity | Informational |
| Status | CLOSED — fixed |
| Discovered | 2026-11-16, during an architecture review of the authorization test evidence |
| Affected component | [`docs/authorization-permissions.md`](authorization-permissions.md#6-proposed-test-vectors), section 6 introduction and vector V22 |
| Related requirement or ADR | [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md); [authorization policy section 7](authorization-permissions.md#7-explicitly-excluded-permissions) (no update or transition permission is granted) |
| Target milestone | Authorization evidence accuracy review |

**Observed behavior (from document and test review).**

- **What the documentation claimed.**
  - Section 6 said every HTTP vector test "asserts both the HTTP response **and** the database state in the dedicated test database".
  - The V22 row ("any HTTP status change for an A case") gave the state assertion "`status` and `version` unchanged" and the status "Passed (HTTP)".
- **What the test does.** The test for V22, `test_no_transition_administrative_or_documentation_routes_offline` in [`tests/test_http_case_api.py`](../tests/test_http_case_api.py):
  - uses the `offline_client` fixture, which points at an unreachable database;
  - sends a well-formed, never-issued token to random-UUID paths;
  - asserts only `404`/`405` with `route_not_found` or `method_not_allowed`.
- **What the test does not do.** It creates no organization, actor or case, and it reads no persisted `status` or `version`.
- **Other vectors.** The blanket statement was also broader than the read vectors V02 and V06, whose "No change" entries are not checked in the database.

**Expected behavior.** Under [section 6 of this register](#6-evidence-provenance-rules), evidence states the level at which it was observed. An offline route-absence check must not be presented as a database-backed state assertion, or as authorization evidence for an operation that does not exist.

**Reproduction and evidence provenance.** This was established by reading the committed documentation and test; it is not a runtime defect. The [HTTP case API results](../tests/http-case-api-results.md) already attribute V22 to the offline test, and they are unchanged.

**Impact.** A reader could overestimate the evidence for update and transition protection. The conclusion itself was correct: no HTTP update or transition route exists. No code, test or authorization behavior was affected.

**Decision and options.** The owner accepted this finding as Informational and directed a documentation correction only. No update endpoint and no new authorization behavior are added.

**Acceptance criteria.**

1. The V22 row states that it proves the absence of the HTTP routes through an offline ASGI test. It must not assert persisted `status`/`version`, and must not demonstrate cross-tenant authorization of an implemented update operation.
2. The section 6 introduction names which vectors' state assertions are checked in the database, keeping the valid claims.
3. No production code, migration or test changes.

**Resolution summary.** [`docs/authorization-permissions.md`](authorization-permissions.md#6-proposed-test-vectors) section 6 now:

- states that V22 is an offline ASGI route-absence test with no state assertion;
- names the database-checked state assertions: V01, V04, V05, V08–V11, V13–V15, V17 and V18;
- names the response-only entries: V03, V07, V10, V15 and V19–V21;
- says that the "No change" entries for V02 and V06 are not checked in the database.

Recorded in the revision that adds this entry (`docs: correct V22 authorization evidence attribution`).

**Verification evidence.**

- The corrected wording was compared line by line with the test bodies in `tests/test_http_case_api.py`:
  - `assert_matches_db` and `world.snapshot()` for the database-checked vectors;
  - the `offline_client` fixture for V22.
- The V22 test was rerun unchanged on 2026-11-16 (offline, no database):
  - selected alone (14 parametrized cases): **14 passed**, 0 failed, 0 skipped, 94 deselected;
  - with the offline HTTP subset (`-m "not db"`): **86 passed**, 0 failed, 0 skipped, 22 deselected (the PostgreSQL-backed tests).
- This is a documentation-only change. No code, test or authorization behavior changed, so no new regression test applies.

**Closure date:** 2026-11-16.

## 6. Evidence provenance rules

- **Public evidence lives in this repository.** Cite committed test files, test reports in [`tests/`](../tests/), evaluation records in [`evaluations/`](../evaluations/), and source links. Private notes, personal schedules, local paths, credentials, and raw scratch output are never cited or copied here.
- **State how the evidence was produced.** Give the environment (for example, a disposable local PostgreSQL version with synthetic data) and the level of the observation (HTTP, Python layer, or SQL). An observation at one level is not presented as evidence at another.
- **Distinguish reproduced from reasoned.** Say whether a behavior was executed and observed, or inferred from reading code. Inferences are labelled as such.
- **Do not overstate existing tests.** Link related tests only with a statement of what they do and do not cover. A finding is not "covered" until a committed test reproduces it.
- **Record unknowns explicitly.** An unresolved or unverified field says `Not established`. It never contains an expected or invented result.

## 7. Closure requirements

A finding moves to a `CLOSED` status only when its record contains:

1. **The resolution or decision**, with a link to the commit, ADR, or document that records it.
2. **Verification evidence** that meets section 6. For a code change, this means a committed test that reproduces the original condition and passes after the change, plus the relevant regression suite with exact executed, passed, failed, and skipped counts.
3. **Any accepted residual risk**, stated in one or two sentences, for `CLOSED — risk accepted`.
4. **The closure date.**

Closed records stay in this register, unchanged except for their closure fields. Historical test reports are not edited to reflect a closure; new evidence is recorded alongside them.

## 8. Template for new findings

Copy the block below, assign the next unused identifier, add a row to the summary table, and fill unknown fields with `Not established`.

```markdown
### FDE-C<n>-<AREA>-<NNN>

**<Title>**

| Field | Value |
|---|---|
| Category | Security / Correctness / Reliability / Performance / Maintainability / Documentation |
| Severity | Critical / High / Medium / Low / Informational |
| Status | OPEN — untriaged |
| Discovered | YYYY-MM-DD, <review, experiment, or validation activity> |
| Affected component | <endpoint, module, table, or document> |
| Related requirement or ADR | <links> |
| Target milestone | <engineering milestone, not a personal schedule> |

**Observed behavior.** <What was observed, under which conditions.>

**Expected behavior.** <What the requirement, contract, or ADR specifies.>

**Reproduction and evidence provenance.** <Environment, level of observation (HTTP, layer, SQL), and how it was produced. Link committed evidence only. State whether a committed test reproduces it.>

**Impact.** <Security, correctness, reliability, or performance impact, and its limits.>

**Decision and options.** Not established.

**Acceptance criteria.**

1. <Verifiable condition>

**Resolution summary:** Not established.  
**Verification evidence:** Not established.  
**Closure date:** Not established.
```
