# AI Triage: First Model Integration

**Status:** Initial integration implementation; actual results are captured after execution.

## Goal

Use a real language model to propose a category, urgency, reason, and next step for a synthetic case. Validate these proposed fields, persist them in `ai_suggestion`, and leave `case_record.status` and `case_record.version` unchanged.

## Boundary

- Source input: an existing fictional case in `case_record` owned by the `northwind` seed organization.
- AI request: one hosted model call with a versioned prompt (`triage-v1`), fixed response contract, 30-second timeout, and no automatic retry.
- Output: four Pydantic-validated fields. Invalid, incomplete, or refused output must not be stored as a usable suggestion.
- Storage: read case state, invoke the model, recheck current case state under a database row lock, and insert a new row into `ai_suggestion` only if the case has not changed.
- Business authority: this integration cannot approve a decision or change case state. It is a local development CLI, **not** an authenticated tenant-aware service.

## Run locally

From the repository root, with PostgreSQL running and the schema and synthetic seed data applied:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements-ai.txt
Copy-Item .env.example .env
```

Edit `.env` locally with the hosted provider key and database URL. Do not commit `.env`, real keys, or customer data. A ChatGPT subscription is not an API balance. Check API billing and usage limits before the run. Avoid repeated requests, since this integration makes a billable API call.

```powershell
.venv\Scripts\python -m pytest tests/test_model_output.py -q
.venv\Scripts\python -m scripts.run_triage
```

The model call occurs only in the second command. If successful, it writes `evaluations/model-run-01.json`. Never hand-author the actual model output or mark a run as passed before observing it.

## Verify database state

```powershell
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -c "SELECT c.title, c.status, c.version, s.proposed_category, s.proposed_urgency, s.model_id, s.prompt_version FROM ai_suggestion s JOIN case_record c ON c.id = s.case_id ORDER BY s.created_at DESC LIMIT 5;"
```

Expected after a successful run: a new suggestion row, while the source case retains its existing status and version. Inspect the JSON evidence for the actual model output, model identifier, prompt version, token usage if available, and elapsed time. Model correctness requires separate labeled-case evaluation; this run only proves invocation, structured validation, and suggestion-only persistence.

## Limitations

- OpenAI-hosted inference is used for this first integration; a local model could be substituted later without changing the business authority boundary.
- The script is not an API server and does not provide tenant authorization, reviewer approval, retries, human workflow, or production credential management.
- A response satisfying a schema can still be factually wrong or propose a poor next step.
- The local CLI is intended to run on fictional/synthetic inputs only.
