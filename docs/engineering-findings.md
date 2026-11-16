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
