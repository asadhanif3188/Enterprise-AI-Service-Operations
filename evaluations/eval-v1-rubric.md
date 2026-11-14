# AI Triage Evaluation — Preliminary Rubric v0.1

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** DRAFT / DESIGN-ONLY — no labeled dataset or evaluation results are claimed  
**Target dataset:** `evaluations/eval-v1.jsonl` (to be authored and frozen in the evaluation-data increment)  
**Evaluation task:** Initial service-case triage, not case resolution or authorization  
**Current prompt and output contract:** `triage-v1`; `src/model_adapter.py` (`TriageSuggestion`)  
**Governing decision:** `docs/adr/0002-ai-suggestions-non-authoritative.md`

## 1. Purpose and scope

The dataset will contain **10–30 independently authored, synthetic service cases** and reference annotations. The first evaluation asks whether the model supplies a valid, useful, appropriately cautious advisory triage response from *only* the case facts provided. It does **not** test whether the model may execute case updates, whether authenticated users are authorized, or whether suggestions are correct in production.

The current adapter accepts exactly these output fields:

| Field | Current permitted values/constraints |
|---|---|
| `category` | `network`, `access`, `application`, `other` |
| `urgency` | `low`, `medium`, `high` |
| `reason` | Nonblank string |
| `suggested_next_step` | Nonblank string |

Additional fields are rejected. Model responses can be schema-valid yet factually unsupported or unhelpful. **The current contract has no explicit `abstain`, `unknown`, or `needs_more_information` output. `other` is a category, not an abstention.** Do not invent an abstention outcome or count an unimplemented abstention feature as a tested model capability.

## 2. Human reference annotation — preliminary JSONL shape

`eval-v1.jsonl` should contain **one complete JSON object per line** when the cases have actually been written and annotated. Proposed shape:

```json
{
  "eval_id": "<stable synthetic example identifier>",
  "dataset_version": "eval-v1",
  "case_input": {
    "title": "<independently authored synthetic title>",
    "description": "<independently authored synthetic description>"
  },
  "reference": {
    "annotation_status": "PENDING",
    "category": null,
    "urgency": null,
    "insufficient_information": null,
    "supporting_spans": [],
    "annotation_rationale": null
  },
  "tags": []
}
```

**Schema illustration only; this is not a dataset item, labeled case, or observed result. Do not copy the placeholder into `eval-v1.jsonl`.** A ready-to-evaluate case must replace the placeholders, use `annotation_status: "FINAL"`, and have human-approved reference fields. If no defensible reference category or urgency exists, leave it `null` and mark that dimension *not scorable*; do not force a guessed label.

`supporting_spans` are exact quotations or position-based references to text **in the supplied synthetic case**, not fabricated web citations. Later retrieval-based evaluation will need a separate reference contract and source permissions.

Suggested tags (overlapping by design): `straightforward`, `ambiguous`, `missing_impact`, `missing_scope`, `multiple_symptoms`, `other_category`, `prompt_injection`, `high_urgency_claim`, `repeat_incident`.

## 3. Proposed scoring rubric

Report dimensions **separately**, with counts and an applicable denominator. Do not collapse them into an overall accuracy number unless the weighting, missing values and rejection policy have been reviewed.

| Dimension | Assessment | How to decide | Not applicable / unresolved |
|---|---|---|---|
| **Schema adherence** | PASS / FAIL | Parse against the *actual* `TriageSuggestion`: all four fields, allowed vocabularies, nonblank text, no extras | Refusal or failed call is a distinct outcome; record it rather than treating as a completed suggestion |
| **Category agreement** | MATCH / MISMATCH | Exact comparison to a human-approved `reference.category` | No defensible single category → `NOT_SCORABLE`, not a guessed match |
| **Urgency agreement** | MATCH / MISMATCH | Exact comparison to a human-approved `reference.urgency` based on stated case impact | Unspecified impact prevents a defensible urgency reference → `NOT_SCORABLE` |
| **Evidence grounding** | SUPPORTED / PARTIAL / UNSUPPORTED | Each substantive reason claim is directly supported by the provided case text; cite input spans when available | Source citation is `NOT_APPLICABLE` when the input contains no citable external document |
| **Next-step usefulness** | 0 / 1 / 2 | `0`: unsafe, irrelevant or presumes nonexistent access; `1`: plausible but vague/conditional; `2`: specific, relevant, safe first investigation step | Record judgment notes; this is a subjective rubric, not a validated metric yet |
| **Uncertainty handling** | CALIBRATED / OVERCLAIMED / INDETERMINATE | In missing-data cases, uncertainty is acknowledged in `reason` and the next step seeks missing evidence, rather than asserting impact/severity | No explicit abstention field exists, so **explicit abstention success is NOT_SUPPORTED** by the current output contract |

### Reference-label rules

- **Category:** use only `network`, `access`, `application`, `other` when case facts support that specific category. `other` is for a supported issue outside the first three categories; it must not stand for unknown.
- **Urgency:** do not label `high` solely because a symptom sounds serious. A defensible urgency judgment needs enough context about scope, impact, affected users/services or business criticality. Exact high/medium/low operational thresholds have **not yet** been established; mark borderline cases `PENDING` or `NOT_SCORABLE` until policy anchors are approved.
- **Insufficient information:** flag when missing facts prevent a defensible category, urgency or proposed action. Track which field is unanswerable in the annotation rationale.
- **Supporting text:** quote only case-provided evidence. If the case does not supply a source, do not invent citations.
- **Safety:** a recommended investigation step must not be interpreted as permission to execute a privileged operation. The model is advisory under ADR-0002.

## 4. Example coverage plan (not data or labels)

For the future 10–30 synthetic cases, aim for coverage of all four category values and a mix of evident-impact, limited-impact, and missing-impact cases. Include some ambiguous/multi-symptom cases, examples where an appropriate answer requests more information, and at least one untrusted instruction embedded within case text. Tags can overlap. No specific case counts, labels, or results are asserted here.

## 5. Evaluation and reporting protocol (future work)

1. **Create synthetic inputs** independently, avoiding real customer data, private tickets, secrets and unapproved source material.
2. **Label before seeing predictions** where practical. Record who produced/reviewed each label (use non-identifying reviewer IDs for a public dataset), rationale, source spans, and any disagreement.
3. **Freeze and version** the dataset and rubric (`eval-v1`). If labels change after review, record the change; don't silently edit a previously reported benchmark.
4. **Run the actual model** with the recorded model ID, prompt version, configuration, time, and input. Keep outputs in a separate run-results artifact, not inside the reference dataset.
5. **Validate schema first**, then score only applicable quality dimensions. A model call error, invalid output, or refusal must be counted and diagnosed separately; do not silently drop failures.
6. **Report raw numerator/denominator**, all non-scorable cases, latency and token usage when observed; distinguish measured quantities from estimated monetary cost. A 10–30-case set has substantial uncertainty and should not support claims of production accuracy.
7. **Review contract evolution** before adding explicit abstention. If introduced, version the model output schema and prompt; do not reinterpret `other` as `abstain` retroactively.

## 6. Questions to settle before the first evaluation run

- What written operational policy distinguishes `low`, `medium` and `high` urgency?
- What is the agreed target when category or urgency cannot be justified by the input?
- Do we need a revised model schema with an explicit abstention/needs-information outcome, or should this first run only measure overclaiming under the existing contract?
- Who reviews ambiguous annotations and what constitutes annotation agreement?
- What exact synthetic fields are exposed to the model, and will future enriched context require new evaluation versions?

## 7. Evaluation lifecycle and evidence status

The evaluation work is divided into three stages, each producing a distinct artifact.

**Stage 1 — Evaluation rubric (preliminary)**

Define the evaluation dimensions, scoring criteria, annotation rules, and unresolved questions.

- Artifact: `evaluations/eval-v1-rubric.md`
- Status: Preliminary design
- Evidence boundary: No reference-labeled evaluation dataset or measured model accuracy is established by this rubric.

**Stage 2 — Versioned evaluation dataset**

Create at least 10 fully synthetic service cases, with human-reviewed reference annotations covering classification, urgency, insufficient information, and supporting evidence where applicable.

- Artifact: `evaluations/eval-v1.jsonl`
- Status: Planned
- Evidence boundary: Reference labels represent evaluation expectations, not observed model performance.

**Stage 3 — Model evaluation and analysis**

Execute the versioned dataset against a recorded model and prompt configuration. Retain actual responses, schema-validation outcomes, latency, usage where available, dimension-level scores, and observed failure modes.

- Artifact: `evaluations/eval-v1-report.md` and separately retained evaluation-run evidence
- Status: Planned
- Evidence boundary: Results must be based on actual executions. Model accuracy, reliability, and cost must not be inferred from the rubric or reference labels alone.

**Related repository documents:**

- `docs/requirements.md`
- `docs/ai-triage-integration.md`
- `docs/data-model.md`
- `evaluations/model-run-01.json`
- `src/model_adapter.py`

The existing single-model experiment establishes an initial integration baseline, not the results of the versioned evaluation dataset.