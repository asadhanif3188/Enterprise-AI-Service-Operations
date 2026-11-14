# Architecture Decision Records

This directory contains the Architecture Decision Records (ADRs) for the Enterprise AI Service Operations & Intelligence Platform. New records use the [template](TEMPLATE.md).

## What an ADR is

An ADR is a short record of one significant architectural decision. It is about one page long and normally no more than two. It states the problem, the constraints, the options considered, the decision and why it was chosen, the risks and consequences accepted, and the conditions under which the decision should be reconsidered.

An ADR explains **why** the system is shaped the way it is. It does not track implementation progress. Current status belongs in the living documents listed under [Accepted is not Implemented](#accepted-is-not-implemented).

## Why this project uses ADRs

- **Visible tradeoffs.** Reviewers can see which alternatives were weighed and which costs were accepted, not only the final choice.
- **Traceability.** Each decision links to the requirements it serves, the code that implements it, and the recorded evidence about it.
- **Durable reasoning.** The rationale stays available after the people, context, or documents around a decision change.
- **Planned reconsideration.** Each ADR names the conditions that should trigger a review, so decisions are revisited deliberately.

## When a new ADR is needed

Write an ADR when a decision:

- Is difficult or costly to reverse, for example the system of record, data ownership, or the consistency model.
- Changes a trust, security, or tenant boundary.
- Changes what is authoritative, or who or what may change it.
- Defines a contract that other components or clients will depend on, such as API, retry, or idempotency semantics.
- Chooses between credible alternatives with meaningful tradeoffs.
- Changes or replaces an existing ADR.

## What does not require an ADR

- Routine implementation choices that can be changed locally, such as naming, internal helpers, or refactoring.
- Dependency updates that do not change the architecture.
- Bug fixes that restore intended behavior.
- Tests and evidence reports. These are evidence, recorded in [`tests/`](../../tests/) and [`evaluations/`](../../evaluations/).
- Detailed operating rules within a decision that an ADR already governs. These belong in design documents, such as [Case creation semantics](../case-creation-semantics.md).

## Lifecycle

| Status | Meaning |
|---|---|
| **Proposed** | A preferred direction exists, but the architectural decision is not fully settled. |
| **Accepted** | The architectural direction is adopted, whether or not it is implemented yet. |
| **Superseded** | A newer ADR replaces the decision. The status line links to the replacement. |
| **Deprecated** | The decision no longer applies, without necessarily having a direct replacement. |

A Proposed ADR that is abandoned is marked Deprecated with the reason, rather than deleted. When an ADR's status changes, update its status line and add the date of the change.

## Accepted is not Implemented

An Accepted ADR records an architectural choice. It does **not** claim that the implementation is complete, that the necessary security controls exist, or that tests have verified the behavior. Some Accepted ADRs in this project describe requirements, such as authorization checks, that the current code does not yet meet. Each ADR states which parts were implemented when it was recorded.

Current implementation status is maintained in living documents, not in ADRs:

- [System context](../system-context.md): capability status and trust boundaries.
- [Architecture context](../architecture-context.md): the current implementation and its boundary.
- The project [README](../../README.md): progress checklist.

Observed results are recorded in test reports and experiment evidence.

## Changing a decision

The substance of an Accepted ADR is not rewritten. When the architectural choice changes:

1. Write a new ADR that states what changed and why, with a **Supersedes** line linking to the earlier ADR.
2. Change the earlier ADR's status to "Superseded by ADR-NNNN", linking to the new record. Leave the rest of its content unchanged.
3. Update the register below and any documents that link to the old decision.

Fixing broken links, typos, or wording that does not alter the original meaning is permitted without a new ADR.

## Retrospective decisions

Some decisions were made and implemented before this ADR process existed. They were first recorded as brief notes (DEC-001 to DEC-003) in the [architecture context](../architecture-context.md). The ADRs that formalize them:

- Are marked **Decision provenance: Retrospective reconstruction**.
- Use the date the ADR was written as **Recorded**, and identify the original note and when it was first committed.
- Present options that were reconstructed for transparent comparison. They do not claim that these options were formally presented, benchmarked, or reviewed when the decision was first made.

## Links to requirements, implementation, and evidence

Each ADR header lists:

- **Related requirements:** functional requirement IDs and rules from the [requirements](../requirements.md).
- **Implementation evidence:** source files, migrations, and recorded test or experiment results, or "None".

Evidence shows what was observed under stated, local conditions. It is not a production guarantee. Evidence reports are never edited to fit an ADR; new observations are recorded as new evidence.

## Register

| ID | Title | Status | Legacy reference |
|---|---|---|---|
| [ADR-0001](0001-postgresql-authoritative-case-store.md) | PostgreSQL as the Authoritative Case Store | Accepted | DEC-001 |
| [ADR-0002](0002-ai-suggestions-non-authoritative.md) | AI Suggestions Are Non-Authoritative | Accepted | DEC-002 |
| [ADR-0003](0003-application-controlled-case-transitions.md) | Application-Controlled Case State Transitions | Accepted | DEC-003 |

## Future Architecture Decisions — Not Yet Recorded

These topics are expected to need decisions as the project grows. They are backlog items, not accepted decisions, and have no ADR numbers yet.

- Authenticated actor identity and organization membership.
- Server-side tenant authorization.
- PostgreSQL row-level security as defense in depth.
- Human-review approval binding to a specific suggestion and case version.
- Idempotency key retention and replay-response rules.
- Durable background work and job recovery.
- Analytical projection freshness and reconciliation.
- Database audit controls and prevention of alternate writers that bypass application rules.
