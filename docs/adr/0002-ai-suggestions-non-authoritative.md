# ADR-0002: AI Suggestions Are Non-Authoritative

- **Status:** Accepted
- **Recorded:** 2026-11-12
- **Decision provenance:** Retrospective reconstruction. This ADR formalizes the earlier note [DEC-002](../architecture-context.md#dec-002--the-ai-component-produces-suggestions-not-business-actions), "The AI component produces suggestions, not business actions", first committed on 2026-11-11. The options below were reconstructed for transparent comparison; they were not formally evaluated when the decision was made.
- **Legacy reference:** DEC-002
- **Related requirements:** FR-04, FR-05, FR-06, FR-08; rules "Source of truth", "Human authorization", and "Graceful AI failure" ([requirements](../requirements.md))
- **Implementation evidence:**
  - Source: [`src/model_adapter.py`](../../src/model_adapter.py), [`scripts/run_triage.py`](../../scripts/run_triage.py).
  - Design: [AI triage integration](../ai-triage-integration.md).
  - Results: [recorded model run](../../evaluations/model-run-01.json) (one synthetic case, one run); [failure-path validation](../../tests/ai-triage-failure-results.md) (18/18 selected tests passed).

## Problem

AI-generated triage recommendations may help an engineer, but they can be incorrect, incomplete, or stale because the case changed while the model was running. They can also be influenced by untrusted case text through prompt injection. Cases belong to customer organizations, and the application cannot allow model output to authorize or execute changes to those records on its own.

## Constraints

- **External, nondeterministic model.** The model is hosted by an external provider (OpenAI for the recorded experiment), and its output can vary between runs.
- **Untrusted input.** Case text is untrusted, yet it is sent to the model.
- **Human decisions and AI-independent operation.** The requirements call for authorized human decisions before consequential changes, and for case handling that continues when AI is unavailable.
- **No quality evidence yet.** Model quality has not been evaluated; there is one recorded run.

## Options Considered

1. **Advisory AI with application-controlled execution**
   - *Benefits:* model output can be validated, stored with its provenance, and reviewed by a person. An AI failure cannot corrupt case state, and deterministic application code keeps authority over consequential actions.
   - *Costs:* a review and approval workflow is needed before suggestions have any business effect, which adds steps and application complexity.
2. **Model-generated business actions**, such as tool calls or commands executed directly
   - *Benefits:* the shortest path to automation.
   - *Costs:* a schema-valid but wrong, or injected, response could change customer records. Permission checks, correctness, and recovery would depend on model behavior, and the effects would be hard to audit or reverse.
3. **Deterministic rule-based triage only**
   - *Benefits:* predictable and testable behavior, with no external dependency and no case data sent to a provider.
   - *Costs:* rules are brittle against free-text problem descriptions. Maintaining classification and next-step rules scales poorly as case variety grows.

## Decision

AI output is non-authoritative. Option 1 is adopted.

- **What the model receives and may suggest.** The model receives only the case information the application chooses to send. It may suggest a category, an urgency, an explanation, and a next step.
- **Validation and storage.** The application validates each response against a strict structured contract: fixed vocabularies, nonblank text, and additional fields rejected. It stores the result in `ai_suggestion` with the model identifier, prompt version, and the case version it was based on.
- **What the model cannot do.** The model does not authorize users, approve its own suggestion, change case status, or execute database commands. It has no database credentials and cannot reach data outside the context the application provides.
- **Separate steps.** Accepting a suggestion and executing a case change are separate steps owned by application logic ([ADR-0003](0003-application-controlled-case-transitions.md)).

Structured validation and provenance are **necessary but not sufficient**. They establish that a suggestion has the expected shape and origin, not that it is correct.

Option 1 was chosen because it lets the project use model assistance while keeping every consequential change under deterministic, reviewable application control.

## Risks and Consequences

- **Review workflow required.** Suggestions have no business effect until a human review and approval workflow exists. That workflow was not implemented when this ADR was recorded.
- **Incorrect but schema-valid suggestions.** Accuracy has not been evaluated. One successful model call does not establish classification accuracy.
- **Stale suggestions.** Each suggestion records its `case_version`. The triage CLI rechecks the case before storing a suggestion, but a freshness check at approval time does not exist yet.
- **Prompt injection and unsafe recommendations.** The prompt marks case text as untrusted data, which reduces but does not prevent manipulation. Keeping AI non-authoritative limits the impact.
- **Model unavailability.** The adapter does not retry. Case operations do not depend on the model, but there is no manual-handling workflow yet.
- **Data sent to the provider.** Case fields are sent without redaction, so only synthetic data may be used.
- **Evaluation and observability.** Useful AI assistance needs labeled evaluation cases, together with tracking of invalid outputs, latency, and cost. None of this exists yet.

## Revisit When

- A specific automated action is proposed, and its permission boundary, deterministic validation, impact, reversibility, failure behavior, and human-approval requirements are explicitly designed. Such a proposal would be a new ADR covering that action only. This trigger does not imply that arbitrary model actions should ever be permitted.
- Real customer data would be sent to a model provider. That would require deciding what case information may be shared and under what controls.
