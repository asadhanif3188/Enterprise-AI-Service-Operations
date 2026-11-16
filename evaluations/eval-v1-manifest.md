# eval-v1 manifest

**Status:** 20/20 references FINAL (owner-approved 2026-11-16); rubric v0.2 approved;
original Day 13 gate **PASS**. Freeze: authorized, to be recorded after this
approval revision validates (see [Freeze record](#freeze-record)).

**Authored:** 2026-11-16 (Asia/Karachi).

**Implementation baseline:** `f7f92ff4c763c95ae298d5d5f8e0a8c566bddd71`.

**Dataset:** [`eval-v1.jsonl`](eval-v1.jsonl), version `eval-v1`, 20 cases.

**Rubric:** [`eval-v1-rubric.md`](eval-v1-rubric.md), v0.2, approved without amendment by AO-001 on 2026-11-16.

**Reference review:** [`eval-v1-reference-review.md`](eval-v1-reference-review.md),
an AI-assisted consolidated review (Day 13B). It is not an approval record.

## Purpose and provenance

Evaluate advisory initial service-case triage using only fictional input facts:
classification, urgency, grounding, next-step usefulness, uncertainty, unsupported
claims and investigation safety. This increment authors reference proposals and
validates artifacts offline; it does not run a benchmark.

Codex independently authored these scenarios and proposed annotations for this
task. They began as AI-authored review candidates. The architecture owner
(AO-001) approved all 20 on 2026-11-16 without label corrections; see the
[approval ledger](#approval-ledger).
No real tickets, customer details, secrets or third-party proprietary examples
were used. The repeated wording in 001 and 013 is an intentional contrast written
for this dataset, not a duplicate imported ticket. An embedded instruction in 018
is deliberately fictional untrusted input.

The original Cycle 1 Week 2 plan calls for 10–30 synthetic inputs and at least ten
labeled cases. This Day 13A increment requires at least ten actual human-reviewed
references before final acceptance. The existing repository rubric filename is
retained (`eval-v1-rubric.md`), rather than adding the alternate name used in the
plan. Day 14 activities are outside this increment.

## Reference coverage (not evaluation results)

| Approved category | Count | IDs (suffix of eval-v1-) |
|---|---:|---|
| network | 5 | 001, 002, 003, 013, 014 |
| access | 5 | 004, 005, 006, 015, 018 |
| application | 4 | 007, 008, 009, 020 |
| other | 3 | 010, 011, 012 |
| null / not scorable | 3 | 016, 017, 019 |

| Approved urgency | Count | IDs (suffix of eval-v1-) |
|---|---:|---|
| high | 5 | 001, 004, 007, 010, 017 |
| medium | 5 | 002, 005, 008, 011, 019 |
| low | 6 | 003, 006, 009, 012, 013, 018 |
| null / not scorable | 4 | 014, 015, 016, 020 |

There are **14** clearly assessable references with both labels, and **6** with
`insufficient_information=true` (014, 015, 016, 017, 019, 020). Category is
scorable for 17/20; urgency for 16/20. These are approved reference
denominators (R=20, C=17, U=16), not performance measurements.

| Overlapping coverage tag | Count |
|---|---:|
| straightforward | 14 |
| ambiguous | 6 |
| missing_impact | 4 |
| missing_scope | 2 |
| multiple_symptoms | 2 |
| clarification_required | 6 |
| other_category | 3 |
| prompt_injection | 1 |
| high_urgency_claim | 2 |
| repeat_incident | 2 |
| unsafe_next_step_trap | 5 |
| workaround | 9 |
| borderline_urgency | 1 |

## Annotation policy and review state

| Status | Count | Meaning |
|---|---:|---|
| PENDING | 0 | Proposed by the author; no architecture-owner approval recorded |
| FINAL | 20 | Owner reviewed the exact input, labels (including any null), spans, uncertainty and rationale |
| Human-approved references | 20 | Approved by AO-001 on 2026-11-16; see the approval ledger |

Annotations must use the original reference shape. Null is an honest unscorable
dimension, not `other`, a model abstention or an annotation failure. Reviewers
must resolve uncertainty using supplied facts and the approved synthetic-only
anchors. They must not invent facts to force a label or balance coverage.
The owner approved the anchors and decided 002 as medium (see the ledger).

A local worksheet at `.local/day13/annotation-review.md` contains every complete
input, proposed labels, exact spans, rationale, tags and blank decision fields.
It is an ignored review aid, not approval evidence; the approval ledger below is
the durable record. A private closeout note is
also excluded locally. No new `.gitignore` rule is required.

### Review history

| Date | Revision reviewed | Source and provenance | Outcome | Changes | Approval effect |
|---|---|---|---|---|---|
| 2026-11-16 (Day 13A) | `f7f92ff` baseline | Codex-authored proposals | 20 proposals authored | Dataset created | None; all PENDING |
| 2026-11-16 (Day 13B) | `16345e5` | Consolidated AI-assisted review: ChatGPT-assisted advisory review plus a Gemini-generated ledger that claims an owner `FINAL` approval. That claim is **unverified** and not treated as sign-off. See the [review record](eval-v1-reference-review.md). | 20/20 label agreement, confirmed by script against this repository, together with exact-span checks | Draft refinement to 003: added the exact span "The only person who tried it moved to an adjacent working desk in two minutes." No label, rationale, tag or input changes. | None; 20 PENDING, 0 FINAL |
| 2026-11-16 (Day 13C) | `e1465ba` | Architecture-owner decision AO-001 (see ledger) | All 20 approved; rubric v0.2 approved | Status only: 20 rows PENDING → FINAL. No label, span, rationale, tag or input changes. | 20 FINAL |

Day 13B dataset checksums. Both are **PROVISIONAL**, computed over the exact
LF-normalized bytes Git stores. They exist only for review traceability and are
not freeze checksums:

- Reviewed revision `16345e5`:
  `675016edd31e4890ae7a8e1b51663aaaafd714dd3dfe5621d19111f8f8659bba`
- After the 003 span refinement (pending approval):
  `e7b843e06ec82171651e3dda359c821b5d8d62259d35eed0dfdc8bd77941faef`

Decisions that were open after Day 13B (all resolved in the approval ledger):

- Approve or correct all 20 references, including every null dimension (014,
  015, 016, 017, 019, 020).
- Decide 002, medium or low.
- Accept or reject the 003 span refinement.
- Decide on rubric v0.2.
- Authorize FINAL status and, separately, the dataset freeze.

### Approval ledger

**Approval record 1 (Day 13C).**

| Field | Value |
|---|---|
| Reviewer ID | `AO-001`, a non-identifying architecture-owner alias. The identity mapping is kept private. |
| Review date | 2026-11-16 (Asia/Karachi) |
| Reviewed artifact revision | `e1465ba05898662dc1c96ae944e9dbbd94a8a35a`, dataset bytes `e7b843e06ec82171651e3dda359c821b5d8d62259d35eed0dfdc8bd77941faef` (provisional checksum above) |
| Cohort | `eval-v1-001` to `eval-v1-020` (all 20) |
| How recorded | The owner submitted a decision table in the working session on 2026-11-16. Its entries were worded as recommendations ("Final recommended entry"); the owner's submission is recorded as adopting them. The assistant (Claude Code) transcribed it here. |

| # | Decision | Owner entry |
|---|---|---|
| 1 | Reference proposals 001–020 | Approve all 20; no label corrections |
| 2 | Case 002 urgency | **Medium.** A scheduled six-person class had a meaningful 25-minute interruption despite an alternative room being available. No universal time threshold is established. |
| 3 | Six insufficient-information references | Confirm all six with their exact null dimensions: 014 urgency; 015 urgency; 016 category and urgency; 017 category; 019 category; 020 urgency |
| 4 | Additional span for 003 | **Accept.** The two-minute desk move is direct evidence of mitigation. |
| 5 | Rubric v0.2, including sections 8.5–8.6 | **Approve** without amendments |
| 6 | Reviewer and date | AO-001; 2026-11-16, Asia/Karachi |
| 7 | Mark references FINAL | Yes, all 20 |
| 8 | Dataset freeze | Yes, once all 20 are FINAL and validation succeeds, with the final SHA-256 recorded |

No values were disputed or corrected. The resulting change is the 20 status
fields (PENDING → FINAL); nothing else in the dataset changed. The Day 13B
AI-assisted reviews, including the Gemini ledger's self-described approval,
remain advisory inputs and are not approval records (rubric 8.6). This ledger
entry is the approval record.

Before changing any further row to FINAL, record the actual reviewer ID, review date,
case ID(s), reviewed artifact revision, accepted/corrected values, and rationale
for disputed decisions here. Use a non-identifying reviewer ID; keep its identity
mapping private if needed. Retain the review's artifact revision through a Git
revision or explicitly labeled provisional checksum so corrections remain
auditable. Empty worksheet fields and generated status strings do not constitute
human review. The validator cannot establish that a human performed a review.

## Output contract and evidence boundary

The current prompt version is `triage-v1`. The authoritative output contract is
[`src/model_adapter.py`](../src/model_adapter.py), class `TriageSuggestion`:

- `category`: network / access / application / other.
- `urgency`: low / medium / high.
- `reason` and `suggested_next_step`: nonblank strings; no extra fields.

There is no explicit abstention or needs-information field. Evaluate textual
uncertainty and clarification separately; explicit abstention success is
NOT_SUPPORTED. A future schema change is a design consideration, not an automatic
defect finding. Model suggestions remain advisory under
[ADR-0002](../docs/adr/0002-ai-suggestions-non-authoritative.md).

Only title and description may enter a future evaluation request. References,
tags and worksheet content must not leak into the model input. Store actual
predictions in a separate versioned run artifact. The unchanged
[`model-run-01.json`](model-run-01.json) records a prior one-case integration
experiment; it is not a prediction or result for any eval-v1 case. No new
predictions, accuracy, latency, tokens, costs or model scores are claimed here.

## Scoring denominators and non-scorable policy

The detailed rules and outcome definitions are in rubric sections 8.3–8.4.
For a later run, predeclare attempted set A and approved reference cohort R;
V is the valid-output subset of R, C is the non-null-category subset of R,
and U is the non-null-urgency subset of R.

| Dimension | Exact future denominator |
|---|---|
| Schema | All valid outputs / all attempts A; additionally valid / (valid + invalid completed candidates). Report errors, refusals and incomplete responses separately. |
| Category | Matches / C, including failed attempts in C; also matches / (C intersect V). Null-category rows excluded as NOT_SCORABLE. |
| Urgency | Matches / U, including failed attempts in U; also matches / (U intersect V). Null-urgency rows excluded as NOT_SCORABLE. |
| Grounding, usefulness, unsupported facts, safety | Score distribution counts / V. Report R minus V as NOT_EVALUABLE. Usefulness mean, if reported, is sum of 0/1/2 scores / V. |
| Uncertainty | Distribution counts / V, plus separate insufficient-information and clear-case strata within V. |

PENDING rows never enter R, C, U or approved-reference semantic denominators.
The approved cohort is now R=20, C=17, U=16. There has been no run; all model
metrics are **not measured**. With zero eligible cases report N/A, never a
percentage. Non-valid outcomes remain visible,
and null references do not turn forced model choices into matches or mismatches.

## Offline validation and acceptance

From the repository root:

```powershell
.venv\Scripts\python -m scripts.validate_eval_dataset
.venv\Scripts\python -m scripts.validate_eval_dataset --require-final
.venv\Scripts\python -m pytest tests/test_eval_dataset.py tests/test_model_output.py tests/test_triage_failures.py -m "not db" -q
```

The default validator checks structure only and returns 0. `--require-final`
also requires at least ten FINAL references; with 20 FINAL it returns **0 / PASS**
(see the Day 13C run below). The final-reference count gate is necessary
but does not prove actual human review, rubric approval or freeze authorization.
Tests use temporary fabricated status variations only to verify the count logic;
they never finalize the source dataset.

Observed final offline test run on 2026-11-16: **82 collected, 78 selected and
executed, 78 passed, 0 failed, 0 errors, 0 skipped, 4 deselected**. The four
deselections are PostgreSQL tests. The 78 comprise 64 dataset validation tests,
4 model-output tests and 10 offline triage-failure tests. No hosted model or
database was called. Windows sandbox temp-directory permissions required a
rerun with filesystem access; the private closeout preserves the failed setup
attempt and exact final command. The structural validator passes independently.

Day 13B rerun on 2026-11-16, after the 003 span refinement, using the same three
test files with `-m "not db"`: **82 collected, 78 selected and executed, 78
passed, 0 failed, 0 errors, 0 skipped, 4 deselected** (the PostgreSQL tests). The
default validator returned 0 (structurally valid). `--require-final` returned
**1 / PARTIAL**, which is expected while 0 references are FINAL; it is neither a
regression nor an acceptance. No hosted model or database was called.

Day 13C run on 2026-11-16, after the owner approval and the 20 status changes,
using the same three test files with `-m "not db"`: **82 collected, 78 selected
and executed, 78 passed, 0 failed, 0 errors, 0 skipped, 4 deselected** (the
PostgreSQL tests). The default validator returned 0. `--require-final` returned
**0 / PASS** with 20 FINAL, approved category denominator 17 and approved urgency
denominator 16. The scripted reconciliation still matches all 20 rows with exact
spans. No hosted model or database was called.

## Limitations and change control

This is a small, intentionally varied synthetic set, not a representative sample
of production traffic or proof of reliability, security, fairness or business
benefit. The same AI author created inputs and proposals; owner review is needed
to challenge shared assumptions. A single injection example does not establish
resistance to adversarial input. Subjective grounding/safety judgments and the
urgency anchors need calibration. The Day 13B AI reviewers assessed the existing
proposals rather than labeling blind, so their agreement does not remove the
shared-assumption risk. The repeated pair is correlated, not two
independent observations for statistical inference. All cases are independently
authored original material; that does not imply statistical independence.

1. While pending, changes are reviewable draft revisions. Preserve stable IDs,
   record material input/label/rubric changes, regenerate the local worksheet,
   and rerun validation. Reopen any affected approval. Never repurpose an ID for
   a different case.
2. Record actual owner approval for at least ten references and the rubric before
   declaring the original Day 13 acceptance gate passed. For a full 20-case
   freeze, approve every included row and obtain explicit freeze authorization.
   An approved subset requires an explicit version/cohort decision and updated
   coverage; pending rows cannot silently disappear.
3. Only after approval and freeze, record the SHA-256 of the exact dataset bytes,
   approved rubric revision, cohort IDs, reviewer decisions and freeze revision.
   The frozen checksum is in the [freeze record](#freeze-record). Any pre-freeze
   checksum used for review provenance must be labeled provisional. The ignored
   local authoring script must not be rerun against the frozen dataset; it would
   regenerate PENDING statuses.
4. Preserve the frozen bytes used by a reported run. Later input, label or scoring
   changes require a new version/revision and change log with reason, affected
   IDs, reviewer and comparison impact. Never silently rewrite a benchmark.
5. Model execution and scoring are a later authorized increment. Keep resulting
   observations separate from reference expectations.

## Freeze record

Authorized by AO-001 (approval record 1, decision 8). The freeze is executed in
a separate revision after this approval revision validates. **Not yet recorded
in this revision.**
