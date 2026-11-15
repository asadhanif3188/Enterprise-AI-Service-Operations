# Enterprise AI Service Operations & Intelligence Platform

An independently developed application for exploring how AI-assisted service operations can be designed, built, and operated with sound software engineering practices.

**Project status:** Early development. The initial PostgreSQL schema and synthetic development data are implemented, and database integrity validation passed 11/11 checks. A local Python AI triage integration is implemented, and one hosted-model inference has been recorded successfully. A local case service applies forward-only status transitions with version checks and transactional event history. Actor identities and customer access grants are stored with database-enforced role-grant eligibility, a local module verifies opaque test credentials, and a central Python authorization layer enforces organization-scoped create, read, and list permissions. No HTTP API calls it yet. The authenticated application API, human-review workflow, tenant authorization, and broader operational capabilities are not yet implemented.

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
- [x] Complete and publish database integrity tests — [11/11 checks passed](tests/schema-integrity-results.md).
- [x] Record and validate a real-model triage response — [one synthetic-case run recorded](evaluations/model-run-01.json).
- [x] Validate AI triage output and failure paths — [18/18 selected tests passed](tests/ai-triage-failure-results.md).
- [x] Implement version-checked case transitions with event history in a local service — [16/16 tests passed](tests/case-operations-results.md) (8 PostgreSQL-backed, 8 offline policy). This is not yet an authenticated or authorized API.
- [x] Implement the actor identity schema, provider/customer role-grant eligibility guards, and trusted local provisioning — [87/87 tests passed](tests/identity-grant-integrity-results.md) (61 PostgreSQL-backed, including concurrency; 26 offline). These tables are not yet used for authorization.
- [x] Implement local opaque credential verification (identity only) — [58/58 tests passed](tests/credential-verification-results.md) (9 PostgreSQL-backed, 49 offline). No HTTP boundary uses it yet.
- [x] Implement centralized case authorization (create, read, list) with a structural no-bypass check — [76/76 tests passed](tests/case-authorization-results.md) (29 PostgreSQL-backed, including revocation interleavings; 47 offline). This is a Python boundary, not yet an HTTP API.
- [x] Independently validate the integrated identity and authorization boundary before HTTP work — [security-readiness results](tests/authorization-boundary-readiness-results.md): adversarial review (no exploitable flaw found in the authorization layer; structural-check gaps fixed), fresh PostgreSQL 16 reproduction, real commit-failure and deadlock tests, and a restricted database-role assessment. Recommendation: go for local HTTP integration, with conditions.
- [x] Expose the authorized case operations through a localhost-only HTTP API (`POST /cases`, `GET /cases/{id}`, `GET /cases`) — [HTTP case API results](tests/http-case-api-results.md): bearer authentication, safe error envelope, and HTTP tenant-isolation tests against PostgreSQL. Keyed idempotent creation (interim `501`), cursor pagination, HTTP status transitions, and database-enforced isolation are not implemented; this is a local demonstration, not a deployable service.
- [ ] Deliver an authorized end-to-end case workflow.
- [ ] Add repeatable application deployment and operational evidence.

The case service enforces an initial forward-only lifecycle (`NEW` → `IN_REVIEW` → `RESOLVED` → `CLOSED`), rejects stale updates, and records each transition in the same transaction as the status change. The organization ID it receives filters queries but is not an authorization check, and the actor label is not a verified identity. The case history covers status transitions only, not a complete audit trail.

This checklist describes **project progress**, not the complete feature plan. It will be updated as working evidence becomes available.

The recorded model run shows that the integration can invoke a hosted model, validate its structured output, and store the result as a suggestion without changing case state. It is a single experiment, not an evaluation of model accuracy or a production-ready AI integration.

## Documentation

- [Requirements](docs/requirements.md) — business problem, expected behavior, invariants, and acceptance checks.
- [Architecture context](docs/architecture-context.md) — initial decisions on data ownership, the AI suggestion boundary, and authorized state changes.
- [Architecture Decision Records](docs/adr/README.md) — numbered architectural decisions, alternatives, consequences, and reconsideration triggers.
- [System context](docs/system-context.md) — C4 system context, technical boundaries, capability status, data flows, and trust boundaries.
- [Data model](docs/data-model.md) — initial relational model for organizations, cases, and AI suggestions, with integrity rules and open decisions.
- [Schema integrity validation](tests/schema-integrity-results.md) — test environment, reproduction steps, and observed results for the initial schema.
- [AI triage integration](docs/ai-triage-integration.md) — suggestion-only integration boundary, run instructions, observed experiment, and limitations.
- [Model output contract tests](tests/test_model_output.py) — offline checks of the structured triage output contract.
- [AI triage failure-path validation](docs/ai-triage-failure-testing.md) — failure scenarios, test setup, and known limitations.
- [Failure-path validation results](tests/ai-triage-failure-results.md) — observed offline and PostgreSQL-backed test results.
- [Controlled case operations](docs/case-operations.md) — local case service, transition policy, trust boundaries, and test setup.
- [Case creation semantics](docs/case-creation-semantics.md) — current non-idempotent case creation and the proposed idempotency-key contract (not yet implemented).
- [Actor and organization membership model](docs/identity-model.md) — identity, affiliation, and customer access grants: schema, eligibility guards, Python-layer authorization, and a local HTTP API.
- [Local credential verification](docs/authentication-authorization-sequence.md#51-implemented-local-credential-resolver) — opaque test credential format, digest store, outcomes, rotation and revocation, and limitations.
- [Case authorization](docs/authentication-authorization-sequence.md#71-implemented-authorized-case-operations) — the authorized-case-operations layer, outcomes, and structural no-bypass check.
- [Case authorization results](tests/case-authorization-results.md) — observed permission-matrix, cross-customer, revocation-concurrency, and structural-check results.
- [HTTP case API results](tests/http-case-api-results.md) — executed HTTP tests: authentication, error mapping, tenant isolation, revocation, failure behavior, and the interim idempotency and pagination rules.
- [Authorization boundary readiness results](tests/authorization-boundary-readiness-results.md) — independent security review, fresh-environment reproduction, credential-revocation timing, database-privilege assessment, and the HTTP readiness recommendation.
- [Credential verification results](tests/credential-verification-results.md) — observed offline and PostgreSQL-backed results for the credential resolver.
- [Identity and role-grant integrity results](tests/identity-grant-integrity-results.md) — observed schema, eligibility, provisioning, and concurrency test results for the identity migration.
- [Case operations validation results](tests/case-operations-results.md) — observed PostgreSQL-backed and offline policy results for transitions, version checks, rollback, and concurrent updates.
- [First model run evidence](evaluations/model-run-01.json) — recorded input, model output, provenance, timing, token usage, and case state for the first hosted-model run.

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
docker run -d --name eaiso-postgres -e POSTGRES_USER=eaiso_dev -e POSTGRES_PASSWORD=local-dev-only -e POSTGRES_DB=eaiso_dev -p 127.0.0.1:5432:5432 -v eaiso_pgdata:/var/lib/postgresql/data postgres:16
```

These credentials are for an isolated local development environment only. Choose different, private credentials for any shared or deployed environment. The port is bound to the local machine, not the network. Ensure TCP port `5432` is available.

Check that the database is ready:

```bash
docker exec eaiso-postgres pg_isready -U eaiso_dev -d eaiso_dev
```

If the container already exists from an earlier session, start it with `docker start eaiso-postgres` instead of running `docker run` again.

### 3. Apply the schema migrations (once each, in order)

For a fresh database, apply the migrations in order:

```bash
docker cp db/001_init.sql eaiso-postgres:/tmp/001_init.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -v ON_ERROR_STOP=1 -f /tmp/001_init.sql
docker cp db/002_case_status_events.sql eaiso-postgres:/tmp/002_case_status_events.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -v ON_ERROR_STOP=1 -f /tmp/002_case_status_events.sql
docker cp db/003_actor_identity_and_grants.sql eaiso-postgres:/tmp/003_actor_identity_and_grants.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -v ON_ERROR_STOP=1 -f /tmp/003_actor_identity_and_grants.sql
```

`001_init.sql` creates `organization`, `case_record`, and `ai_suggestion`. `002_case_status_events.sql` adds `case_status_event`, the status-transition history used by the [local case service](docs/case-operations.md). `003_actor_identity_and_grants.sql` adds `actor` and `organization_membership` with role-grant eligibility guards ([identity model](docs/identity-model.md#107-the-first-identity-migration-implemented)). **Do not rerun a migration against a database where it has already been applied.**

If your development database was initialized before `002_case_status_events.sql` existed, check whether the event table is present:

```bash
docker exec eaiso-postgres psql -U eaiso_dev -d eaiso_dev -c "SELECT to_regclass('public.case_status_event');"
```

An empty result means the table is missing: apply only the two `002_case_status_events.sql` commands above. Do not reapply `001_init.sql`. Check for migration 003 the same way with `SELECT to_regclass('public.organization_membership');` and, if it is missing, apply only the two `003_actor_identity_and_grants.sql` commands. The dedicated `eaiso_test` database used by the database-backed tests needs the same migrations; see [Controlled case operations](docs/case-operations.md#running-tests-against-the-dedicated-database) and the [identity test results](tests/identity-grant-integrity-results.md#reproduction).

### 4. Load synthetic development data

```bash
docker cp db/seed_dev.sql eaiso-postgres:/tmp/seed_dev.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -v ON_ERROR_STOP=1 -f /tmp/seed_dev.sql
```

The seed file provides fictional organizations and sample cases. Avoid rerunning it unless you have checked its duplicate-handling behavior. Its duplicate checks apply to development data only; see [Case creation semantics](docs/case-creation-semantics.md#22-development-seed-script).

Optionally, provision the fictional identities (requesters Ahmed and Fatima, provider engineer Ali and manager Sara for both organizations, and provider administrator Bilal with no grants). This needs migration 003 and the Python environment from the [AI triage section](#1-create-a-python-environment):

```powershell
.venv\Scripts\python -m scripts.provision_dev_identities --database-url postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_dev
```

This is a trusted local operator tool, not authenticated self-service: it accepts only a local `eaiso_dev` or `eaiso_test` database and creates no credentials. Rerunning it is safe. Matching actors and grants are left unchanged, and any difference stops the run instead of being overwritten.

To issue a local test credential for one of those actors (the provisioning command prints their IDs):

```powershell
.venv\Scripts\python -m scripts.manage_test_credentials issue --actor-id <actor-uuid> --database-url postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_dev
```

The credential is printed **once** and is not stored anywhere; only its SHA-256 digest goes into the git-ignored `.local/test-credentials.json`. Keep it out of version control, issues, and logs. Use `rotate`, `revoke`, `remove`, and `list` with `--credential-id` to manage it. The credential identifies an actor; the [authorized case operations](docs/authentication-authorization-sequence.md#71-implemented-authorized-case-operations) layer uses it to authorize create, read, and list, and the [local HTTP API](#local-development-http-case-api) accepts it as a bearer credential ([credential details](docs/authentication-authorization-sequence.md#51-implemented-local-credential-resolver)).

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

The SQL migrations establish the relational model and its structural constraints. They do not, by themselves, enforce the application-level transition policy or protect against every concurrent update. The local [case service](docs/case-operations.md) implements version-checked forward transitions and transactional event history, with [16 recorded passing tests](tests/case-operations-results.md). Migration 003 adds database guards so that only ADR-0006-eligible customer access grants can be stored ([87 recorded passing tests](tests/identity-grant-integrity-results.md)). Authentication, tenant authorization, and reviewer approval remain unimplemented, and database clients with unrestricted write access can bypass the service. A superuser, or a writer that disables triggers, can also bypass the guards.

## Local development: AI triage

The AI triage integration is a local command-line experiment, not an application service. It reads the synthetic `northwind` case "Employees cannot connect to VPN", sends it to a hosted model, validates the structured response, and inserts the recommendation into `ai_suggestion`. It does not change the case's `status` or `version`. See [AI triage integration](docs/ai-triage-integration.md) for the design boundary and limitations.

Before you start:

- **The PostgreSQL database must already be initialized.** Complete steps 2–4 of the PostgreSQL setup above. The script stops if the synthetic seed case is not found.
- **Each run makes one billable API call.** You need your own OpenAI API key with API billing enabled. Check your usage limits before running.
- **Never commit the API key.** It belongs only in your local `.env` file, which `.gitignore` excludes.

The commands below use Windows PowerShell and run from the repository root.

### 1. Create a Python environment

```powershell
py -m venv .venv
```

The recorded run used Python 3.12.6.

### 2. Install dependencies

```powershell
.venv\Scripts\python -m pip install -r requirements-ai.txt
```

### 3. Create a local configuration file

```powershell
Copy-Item .env.example .env
```

### 4. Configure local settings

Edit `.env` and set:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Your own OpenAI API key. Required. |
| `OPENAI_MODEL` | Model to request. Defaults to `gpt-4o-mini` if unset; the recorded run requested `gpt-4o-mini`. |
| `DATABASE_URL` | PostgreSQL connection string. The example value matches the local container from the PostgreSQL setup above. |

If a key is ever exposed, revoke it with the provider and create a new one.

### 5. Run the offline tests

```powershell
.venv\Scripts\python -m pytest tests/test_model_output.py tests/test_triage_failures.py -m "not db" -q
```

These tests check the Pydantic output contract with fixed inputs and simulate provider and dependency failures (timeout, refusal, incomplete response, existing evidence file). They make no model or database calls and are not evidence of a real-model run. The `-m "not db"` option excludes the PostgreSQL-backed tests, which need a separate `eaiso_test` database; see [AI triage failure-path validation](docs/ai-triage-failure-testing.md).

### 6. Run a triage experiment

`evaluations/model-run-01.json` already contains the first recorded run and must not be overwritten. The script refuses to write to an existing evidence file and stops before calling the model, so a new experiment needs a different path:

```powershell
.venv\Scripts\python -m scripts.run_triage --evidence evaluations/model-run-02.json
```

Running `python -m scripts.run_triage` without `--evidence` uses the default path, `evaluations/model-run-01.json`, and therefore stops without making a model call.

On success, the script prints the new suggestion ID, whether the case state was unchanged, the elapsed model request time, and the evidence file path. Each successful run adds a new row to `ai_suggestion`; earlier suggestions are not overwritten.

Review a new evidence file before committing it. It contains the prompt, the case input, and the raw model output, so commit only evidence generated from synthetic data.

### 7. Verify the stored suggestion

```powershell
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -c "SELECT c.title, c.status, c.version, s.proposed_category, s.proposed_urgency, s.model_id, s.prompt_version FROM ai_suggestion s JOIN case_record c ON c.id = s.case_id ORDER BY s.created_at DESC LIMIT 5;"
```

A successful run adds a suggestion row with the model identifier and prompt version `triage-v1`. The source case keeps its existing status and version.

### 8. Review the recorded experiment evidence

[`evaluations/model-run-01.json`](evaluations/model-run-01.json) records the requested and actual model, prompt version and instructions, synthetic input, raw model output, validated recommendation, response status, elapsed time, token usage, runtime environment, and case state before and after the write. [AI triage integration — Observed experiment](docs/ai-triage-integration.md#observed-experiment) summarizes it.

One recorded run demonstrates model invocation, structured-output validation, and suggestion-only persistence. It does not establish model accuracy, a latency baseline, or tenant authorization.

## Local development: HTTP case API

A localhost-only FastAPI application exposes `POST /cases`, `GET /cases/{id}`, and `GET /cases` through the authorized case operations ([design](docs/authentication-authorization-sequence.md#72-implemented-local-http-api), [interim contract](docs/api-contract.md#31-implemented-interim-behavior), [results](tests/http-case-api-results.md)). It is a controlled local demonstration with synthetic data and test credentials, not a deployable service: the development database connection may be a superuser, PostgreSQL row-level security is absent, and it serves plain HTTP.

1. Install the API dependencies into the Python environment from the [AI triage section](#1-create-a-python-environment):

   ```powershell
   .venv\Scripts\python -m pip install -r requirements-api.txt
   ```

2. Apply migrations 001–003, provision identities, and issue a credential as described in [step 4 above](#4-load-synthetic-development-data). The server reads `DATABASE_URL` from the environment or `.env`, and the credential store from `EAISO_CREDENTIALS_FILE` (default `.local/test-credentials.json`). Startup fails if either is missing or invalid.

3. Run the server on the loopback interface only:

   ```powershell
   .venv\Scripts\python -m scripts.run_local_api          # http://127.0.0.1:8000
   ```

   The script always binds `127.0.0.1`. Do not start the application with `uvicorn --host 0.0.0.0` or behind any public ingress.

4. Call it with the credential (PowerShell; replace the placeholders):

   ```powershell
   $h = @{ Authorization = "Bearer <credential>" }
   Invoke-RestMethod -Method Post http://127.0.0.1:8000/cases -Headers $h -ContentType application/json `
     -Body '{"organization_id": "<organization-uuid>", "title": "Synthetic VPN outage", "description": "Fictional detail"}'
   Invoke-RestMethod "http://127.0.0.1:8000/cases?organization_id=<organization-uuid>&limit=20" -Headers $h
   ```

Interim limits: any `Idempotency-Key` header gets `501` and creates nothing (keyed creation, ADR-0004, is not implemented), so an ambiguous failure followed by a retry can create a duplicate case. Lists report `has_more` but have no cursor. There are no status-transition, approval, or administrative routes.

## Data and evidence

All sample organizations, tickets, knowledge sources, and evaluation cases are fictional or independently created. Laboratory results will be labeled as such; they should not be mistaken for production traffic, customer outcomes, or independently verified security assurances.

## License

A license has not yet been selected. Public visibility of this repository does not, by itself, grant permission to reuse its contents. The repository's licensing terms will be stated explicitly after the publication and rights review.
