# Architecture Context

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** Initial architecture decision baseline

This document records the starting boundaries between the application and its AI component. These decisions guide the first implementation and may be revisited when requirements or evidence change.

## DEC-001 — The application owns service-case data and workflow decisions

The application is responsible for creating and storing service cases, identifying their owning organization, enforcing valid state transitions, and recording who made each decision. It also controls which users and services are allowed to read or change a case.

PostgreSQL has been selected as the initial system of record. The current schema stores organizations, service cases, and AI suggestion records. Case history and review outcomes will be modeled in later increments. The application will access that data through defined business operations, not through model-generated commands.

**Why:** Case ownership, authorization, and status must remain consistent and auditable even when AI is unavailable.

## DEC-002 — The AI component produces suggestions, not business actions

The AI component receives the case information that the application permits it to use. It can suggest a category, urgency, explanation, and next step. Its response must be validated against an expected structure and recorded with enough information to identify the model and prompt version.

The AI component does not approve changes, assign permissions, update case status, or directly write to the operational database.

**Why:** Model responses can be incomplete, incorrect, or inconsistent. They are useful inputs for review, not trusted instructions.

## DEC-003 — Only authorized application operations can change case state

A model's recommendation does not determine whether a case is new, in review, resolved, or closed. Case state changes only when application rules permit the transition and the required authorization has been checked. Where human approval is required, the application must verify that the approval still applies to the exact proposed change.

An AI suggestion may inform a person's decision, but accepting a suggestion and executing a state change are separate steps.

**Why:** This prevents a generated response, stale suggestion, or repeated request from silently becoming an unauthorized business action.

---

**Current boundary:** These are design decisions, not claims that the controls are already implemented. The first schema and tests will establish the initial correctness checks; authentication, authorization, approval binding, and failure handling will be verified as their corresponding features are built.
