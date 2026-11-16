# eval-v1 manifest — review candidate

**Status:** DRAFT; not frozen; original Day 13 gate **PARTIAL**.

**Authored:** 2026-11-16 (Asia/Karachi).

**Implementation baseline:** `f7f92ff4c763c95ae298d5d5f8e0a8c566bddd71`.

**Dataset:** [`eval-v1.jsonl`](eval-v1.jsonl), version `eval-v1`, 20 cases.

**Rubric:** [`eval-v1-rubric.md`](eval-v1-rubric.md), v0.2 proposal, owner approval pending.

## Purpose and provenance

Evaluate advisory initial service-case triage using only fictional input facts:
classification, urgency, grounding, next-step usefulness, uncertainty, unsupported
claims and investigation safety. This increment authors reference proposals and
validates artifacts offline; it does not run a benchmark.

Codex independently authored these scenarios and proposed annotations for this
task. They are AI-authored review candidates, **not human-approved ground truth**.
No real tickets, customer details, secrets or third-party proprietary examples
were used. The repeated wording in 001 and 013 is an intentional contrast written
for this dataset, not a duplicate imported ticket. An embedded instruction in 018
is deliberately fictional untrusted input.

The original Cycle 1 Week 2 plan calls for 10–30 synthetic inputs and at least ten
labeled cases. This Day 13A increment requires at least ten actual human-reviewed
references before final acceptance. The existing repository rubric filename is
retained (`eval-v1-rubric.md`), rather than adding the alternate name used in the
plan. Day 14 activities are outside this increment.

## Candidate coverage (not evaluation results)

| Proposed category | Count | IDs (suffix of eval-v1-) |
|---|---:|---|
| network | 5 | 001, 002, 003, 013, 014 |
| access | 5 | 004, 005, 006, 015, 018 |
| application | 4 | 007, 008, 009, 020 |
| other | 3 | 010, 011, 012 |
| null / not scorable | 3 | 016, 017, 019 |

| Proposed urgency | Count | IDs (suffix of eval-v1-) |
|---|---:|---|
| high | 5 | 001, 004, 007, 010, 017 |
| medium | 5 | 002, 005, 008, 011, 019 |
| low | 6 | 003, 006, 009, 012, 013, 018 |
| null / not scorable | 4 | 014, 015, 016, 020 |

There are **14** clearly assessable proposals with both labels, and **6** with
`insufficient_information=true` (014, 015, 016, 017, 019, 020). Category is proposed
as scorable for 17/20; urgency for 16/20. These are potential reference
denominators, not approved denominators or performance measurements.

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
| PENDING | 20 | Proposed by the author; no architecture-owner approval recorded |
| FINAL | 0 | Owner reviewed the exact input, labels (including any null), spans, uncertainty and rationale |
| Human-approved references | 0 | No review has yet occurred |

Annotations must use the original reference shape. Null is an honest unscorable
dimension, not `other`, a model abstention or an annotation failure. Reviewers
must resolve uncertainty using supplied facts and the proposed synthetic-only
anchors. They must not invent facts to force a label or balance coverage.
The anchors and borderline medium/low judgment in 002 await owner approval.

A local worksheet at `.local/day13/annotation-review.md` contains every complete
input, proposed labels, exact spans, rationale, tags and blank decision fields.
It is an ignored review aid, not approval evidence. A private closeout note is
also excluded locally. No new `.gitignore` rule is required.

### Approval ledger

**No approval records.** Rubric v0.2: PENDING. Freeze authorization: PENDING.

Before changing any row to FINAL, record the actual reviewer ID, review date,
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
Currently R=0, C=0, U=0 and there has been no run; all model metrics are **not
measured**. With zero eligible cases report N/A, never a percentage. If every
current proposal were eventually approved unchanged, R=20, C=17 and U=16; that
is a conditional coverage statement only. Non-valid outcomes remain visible,
and null references do not turn forced model choices into matches or mismatches.

## Offline validation and acceptance

From the repository root:

```powershell
.venv\Scripts\python -m scripts.validate_eval_dataset
.venv\Scripts\python -m scripts.validate_eval_dataset --require-final
.venv\Scripts\python -m pytest tests/test_eval_dataset.py tests/test_model_output.py tests/test_triage_failures.py -m "not db" -q
```

The default validator checks structure only and returns 0 for this valid pending
draft. `--require-final` also requires at least ten FINAL references and currently
returns **1 / PARTIAL**, as intended. The final-reference count gate is necessary
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

## Limitations and change control

This is a small, intentionally varied synthetic set, not a representative sample
of production traffic or proof of reliability, security, fairness or business
benefit. The same AI author created inputs and proposals; owner review is needed
to challenge shared assumptions. A single injection example does not establish
resistance to adversarial input. Subjective grounding/safety judgments and the
urgency anchors need calibration. The repeated pair is correlated, not two
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
   **No frozen checksum is recorded now.** Any pre-freeze checksum used for review
   provenance must be labeled provisional.
4. Preserve the frozen bytes used by a reported run. Later input, label or scoring
   changes require a new version/revision and change log with reason, affected
   IDs, reviewer and comparison impact. Never silently rewrite a benchmark.
5. Model execution and scoring are a later authorized increment. Keep resulting
   observations separate from reference expectations.
