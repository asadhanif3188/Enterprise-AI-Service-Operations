# ADR-0001: PostgreSQL as the Authoritative Case Store

- **Status:** Accepted
- **Recorded:** 2026-11-14
- **Decision provenance:** Retrospective reconstruction. This ADR formalizes the earlier note [DEC-001](../architecture-context.md#dec-001--the-application-owns-service-case-data-and-workflow-decisions), "The application owns service-case data and workflow decisions", first committed on 2026-11-11. The options below were reconstructed for transparent comparison; they were not formally evaluated or benchmarked when PostgreSQL was selected.
- **Legacy reference:** DEC-001
- **Related requirements:** FR-01, FR-02, FR-03, FR-09; rules "Organization ownership", "Source of truth", "Concurrency", and "Reporting boundaries" ([requirements](../requirements.md))
- **Implementation evidence:**
  - Migrations: [`db/001_init.sql`](../../db/001_init.sql), [`db/002_case_status_events.sql`](../../db/002_case_status_events.sql).
  - Model: [data model](../data-model.md).
  - Results: [schema integrity validation](../../tests/schema-integrity-results.md) (11/11 checks passed); [case operations validation](../../tests/case-operations-results.md) (16/16 tests passed).

## Problem

The platform needs one authoritative store for customer organizations, service cases, their current workflow state, advisory AI suggestions, and status-transition history. A status change and its transition record must commit or fail together, and concurrent updates must not silently overwrite each other. Without a single authority, model output, reports, or other copies of the data could be mistaken for a case's real state.

## Constraints

- **Local, synthetic environment:** development runs on one machine, with PostgreSQL 16 in Docker and synthetic data for two fictional organizations.
- **Relational data:** every case belongs to exactly one organization, and suggestions and status events belong to a specific case and case version.
- **Required integrity features:** foreign keys, check constraints, multi-statement transactions, row locking, and inspectable history.
- **Minimal infrastructure:** the project adds infrastructure only when there is a reason for it.
- **Derived views have no authority:** future analytical views must not be able to authorize or replace a workflow change (rule "Reporting boundaries").

## Options Considered

1. **PostgreSQL as a relational transactional (OLTP) database**
   - *Benefits:* ownership and relationships are expressed as foreign keys. Status and version rules are expressed as constraints. ACID transactions can span a case update and its event. `SELECT ... FOR UPDATE` and version-guarded updates support concurrency control. It is one mature service to run, and the existing schema already uses it.
   - *Costs:* schema changes need versioned migrations. The database must be operated, backed up, and recovered. Query performance has to be measured as workloads grow.
2. **Document database**
   - *Benefits:* a flexible case shape, and a case could be stored together with its history in one document.
   - *Costs:* many document databases support multi-document transactions, but ownership rules and invariants that span records (organization, case, suggestion, event) would rely more on application code than on declarative foreign keys and constraints. Relational queries for reporting are harder. For this workload's relational shape, that means more application complexity for the same integrity guarantees.
3. **Event store as the only authority (event sourcing)**
   - *Benefits:* complete temporal history by design, and state can be rebuilt by replay.
   - *Costs:* current state must be rebuilt through projections. That introduces projection lag, event-schema versioning, and replay-correctness work that the current scope does not need. It still requires a store with concurrency control.

## Decision

Adopt PostgreSQL as the initial authoritative system of record, owned and accessed by the application.

- `case_record` holds each case's owning organization and its authoritative current state (`status` and `version`).
- `case_status_event` records status changes made through the application's case service ([ADR-0003](0003-application-controlled-case-transitions.md)).
- `ai_suggestion` stores advisory model output tied to a case version ([ADR-0002](0002-ai-suggestions-non-authoritative.md)). It is never case state.
- The application reads and changes this data through defined business operations, not through model-generated commands. It is responsible for controlling which users and services may read or change a case. **This access control is part of the decision but was not implemented when this ADR was recorded.**
- An analytical store, if one is introduced later, is a derived projection. It cannot authorize or replace a transactional update.

PostgreSQL was chosen because its relational model and integrity mechanisms (foreign keys, constraints, and transactions) directly express the ownership, validity, and consistency rules the requirements demand. It fits this workload with less additional application complexity and infrastructure than the alternatives.

## Risks and Consequences

- **Central dependency.** If the database is unavailable, case operations stop. Backup, recovery, and monitoring procedures have not yet been defined.
- **Migration discipline.** Schema changes require forward-only, versioned migrations. A migration that has already been applied is not edited.
- **Unmeasured performance.** No workload has been measured, so performance limits are unknown.
- **Structure, not process.** Constraints enforce data structure, not business process. Any client with write privileges can bypass application rules such as the transition policy. The local components use the development database account, and no least-privilege roles are defined.
- **No tenant authorization.** PostgreSQL does not provide tenant authorization by itself. When this ADR was recorded, the application filtered queries by a caller-supplied organization ID, which is not authorization. Server-side authorization, and possibly row-level security as defense in depth, remain future decisions.
- **Partial history.** Only status transitions are recorded. Case creation, content edits, and review decisions are not.

## Revisit When

- Representative workload measurements show that the design cannot meet documented requirements after reasonable optimization, such as indexing, query changes, or connection management.
- Multiple independent state-changing writers introduce consistency requirements that a single relational authority handles poorly.
- Business requirements fundamentally change the shape of the data, for example if cases stop being records owned by one organization.
- A proposed alternative shows measured benefits large enough to justify its additional complexity.

No numeric thresholds are set, because no workload has been measured.
