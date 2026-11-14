# API Contract

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** Draft. **No HTTP API is implemented.** This document will define the contract for case creation and retrieval before any endpoint is built. Endpoint details are not yet specified.

## Governing Architecture Decisions

The API contract must conform to the following [Architecture Decision Records](adr/README.md). Each row states what the decision requires of the API.

| ADR | Status | Implication for the API contract |
|---|---|---|
| [ADR-0001: PostgreSQL as the Authoritative Case Store](adr/0001-postgresql-authoritative-case-store.md) | Accepted | Case creation and retrieval operate against PostgreSQL as the authoritative store. API responses report the case state recorded there. |
| [ADR-0002: AI Suggestions Are Non-Authoritative](adr/0002-ai-suggestions-non-authoritative.md) | Accepted | `POST /cases` creates a case without requiring AI inference. AI output cannot authorize a request or change case state. |
| [ADR-0003: Application-Controlled Case State Transitions](adr/0003-application-controlled-case-transitions.md) | Accepted | The existing `transition_case()` function must not be exposed through an HTTP endpoint until authentication, tenant authorization, and approval checks exist. The service currently performs none of these checks. |
| [ADR-0004: Keyed Idempotent Case Creation](adr/0004-keyed-idempotent-case-creation.md) | **Proposed** | The retry contract for `POST /cases` is designed against this proposal, but it is **Proposed and not implemented**. No part of this contract may claim that case creation is idempotent or duplicate-safe until the tests listed in the ADR have recorded observed results. |

**Accepted does not mean implemented.** ADR-0001 to ADR-0003 include requirements, such as access control and authorization, that the current code does not meet. See the [system context](system-context.md#4-capability-status) for current capability status.
