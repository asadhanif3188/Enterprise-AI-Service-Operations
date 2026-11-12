# Enterprise AI Service Operations & Intelligence Platform

An independently developed application for exploring how AI-assisted service operations can be designed, built, and operated with sound software engineering practices.

**Project status:** Early development. The initial PostgreSQL schema and synthetic development data are available. Application services and AI integration are not yet implemented. Database integrity testing is in progress.

## The problem

A technology service provider supports several customer organizations. When a support case arrives, an engineer needs to understand the issue, assess its urgency, look for relevant knowledge, decide on the next step, and keep an accurate record of the outcome.

This process can be repetitive, and the information needed to investigate a case may be spread across different records and documents. AI can help prepare an initial recommendation, but its output cannot be treated as a business decision. The application must also protect each organization's information and remain usable when AI or another dependency fails.

I'm building this project to work through those problems as one connected system: from requirements and data modeling to implementation, security, testing, deployment, and operations.

## What the application is intended to do

The first complete workflow will allow an authorized user to:

1. Create a service case for a customer organization.
2. Request a structured AI triage suggestion, including a proposed category, urgency, and next step.
3. Review, correct, approve, or reject the suggestion.
4. Apply an authorized case update through application logic—not directly through model output.
5. Inspect the case history and processing outcome.
6. Continue handling the case when AI is unavailable.

Later work will explore authorized knowledge retrieval, durable background processing, operational reporting, and simulated external integrations. These capabilities will be introduced incrementally and tested against explicit requirements.

## Engineering questions

The project is organized around practical questions rather than a predetermined technology stack:

- How should transactional state, AI suggestions, and analytical views be separated?
- How do we enforce organization-level access across APIs, background jobs, and retrieved context?
- What happens when a worker crashes, an action is retried, or an external call times out after taking effect?
- How do we measure whether AI recommendations are useful, safe, and worth their cost?
- How can the system be deployed, monitored, changed, and recovered reproducibly?

Design decisions will be recorded with their assumptions and trade-offs. Tests and experiments will be published when they can be reproduced, with observed results clearly distinguished from estimates.

## Scope

This is a focused engineering case study, **not** a full IT service-management product. The initial implementation uses fictional organizations, synthetic service cases, and a limited set of business actions.

The project does not require a message broker, a separate analytical database, a GPU cluster, or microservices simply to demonstrate those technologies. Architectural complexity needs a reason.

No real customer data or private third-party project materials are used.

## Current progress

- [x] Define initial business requirements and architecture boundaries.
- [x] Design the initial relational data model.
- [x] Implement the initial PostgreSQL migration.
- [x] Add synthetic development seed data.
- [ ] Complete and publish database integrity tests.
- [ ] Record and validate a real-model triage response.
- [ ] Deliver an authorized end-to-end case workflow.
- [ ] Add repeatable application deployment and operational evidence.

This checklist describes **project progress**, not the complete feature plan. It will be updated as working evidence becomes available.

## Documentation

- [Requirements](docs/requirements.md) — business problem, expected behavior, invariants, and acceptance checks.
- [Architecture context](docs/architecture-context.md) — initial decisions on data ownership, the AI suggestion boundary, and authorized state changes.
- [Data model](docs/data-model.md) — proposed relational model for organizations, cases, and AI suggestions, with integrity rules and open decisions.

Architecture decisions, test evidence, evaluation results, and operating instructions will be added alongside the relevant implementation work.

## Local development: PostgreSQL

The application services are not yet available. The PostgreSQL development database can be initialized using the migration and synthetic seed scripts below. These instructions cover the database foundation, not the complete application.

The commands below assume Docker is installed and running, and that you are executing them from the repository root. The commands work in PowerShell or a Bash-compatible shell.

### 1. Clone the repository

```bash
git clone https://github.com/asadhanif3188/Enterprise-AI-Service-Operations.git
cd Enterprise-AI-Service-Operations
```

### 2. Start PostgreSQL

```bash
docker run -d --name eaiso-postgres -e POSTGRES_USER=eaiso_dev -e POSTGRES_PASSWORD=local-dev-only -e POSTGRES_DB=eaiso_dev -p 127.0.0.1:5432:5432 -v eaiso_pgdata:/var/lib/postgresql/data postgres:17
```

These credentials are for an isolated local development environment only. Choose different, private credentials for any shared or deployed environment. The port is bound to the local machine, not the network. Ensure TCP port `5432` is available.

Check that the database is ready:

```bash
docker exec eaiso-postgres pg_isready -U eaiso_dev -d eaiso_dev
```

If the container already exists from an earlier session, start it with `docker start eaiso-postgres` instead of running `docker run` again.

### 3. Apply the initial schema (once per fresh database)

```bash
docker cp db/001_init.sql eaiso-postgres:/tmp/001_init.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -v ON_ERROR_STOP=1 -f /tmp/001_init.sql
```

This migration creates `organization`, `case_record`, and `ai_suggestion`. It is intended for a fresh database; **do not rerun it against an already initialized database**.

### 4. Load synthetic development data

```bash
docker cp db/seed_dev.sql eaiso-postgres:/tmp/seed_dev.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -v ON_ERROR_STOP=1 -f /tmp/seed_dev.sql
```

The seed file provides fictional organizations and sample cases. Avoid rerunning it unless you have checked its duplicate-handling behavior.

### 5. Verify the data

```bash
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -c "SELECT o.slug AS organization, c.title, c.status, c.version FROM case_record c JOIN organization o ON o.id = c.organization_id ORDER BY o.slug, c.title;"
```

You should see sample cases linked to their owning organizations, with the status and version assigned by the schema or seed script. To inspect the table definitions interactively:

```bash
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev
```

In `psql`, run `\dt`, `\d case_record`, and `\q` to inspect tables and then exit.

### 6. Stop and resume the environment

```bash
docker stop eaiso-postgres
```

To resume later:

```bash
docker start eaiso-postgres
```

Data persists in the Docker named volume `eaiso_pgdata`. A normal container stop does not delete it. **Do not remove the volume unless you intend to discard local data.**

The SQL scripts establish and populate the relational model; they do **not** yet prove tenant authorization, lifecycle transition enforcement, or safe concurrent updates. Those controls will be added and verified separately.

## Data and evidence

All sample organizations, tickets, knowledge sources, and evaluation cases are fictional or independently created. Laboratory results will be labeled as such; they should not be mistaken for production traffic, customer outcomes, or independently verified security assurances.

## License

A license has not yet been selected. Public visibility of this repository does not, by itself, grant permission to reuse its contents. The repository's licensing terms will be stated explicitly after the publication and rights review.
