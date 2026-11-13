# AI Triage: First Model Integration

**Status:** Initial integration implemented and exercised once on a synthetic case. See [Observed experiment](#observed-experiment).

## Goal

The integration uses a real hosted language model to propose a category, urgency, reason, and next step for a synthetic case. It validates the proposed fields, persists them in `ai_suggestion`, and leaves `case_record.status` and `case_record.version` unchanged.

## Boundary

- **Source input:** an existing fictional case in `case_record` owned by the `northwind` seed organization.
- **AI request:** one hosted model call with a versioned prompt (`triage-v1`), a fixed response contract, a 30-second timeout, and no automatic retry. The prompt tells the model that the case text is untrusted data and that it must not claim to resolve a case, approve an action, or change its status.
- **Output contract:** four Pydantic-validated fields (see [`src/model_adapter.py`](../src/model_adapter.py)). `category` must be `network`, `access`, `application`, or `other`; `urgency` must be `low`, `medium`, or `high`; `reason` and `suggested_next_step` must not be blank; any additional field is rejected. Invalid, incomplete, or refused output is not stored.
- **Storage:** the script reads the case, invokes the model, rechecks the current case state under a shared row lock, and inserts a new row into `ai_suggestion` only if the case has not changed. The model's `reason` is stored in the `reasoning` column.
- **Business authority:** the model has no database access. Only application-controlled code writes, and it writes only to `ai_suggestion`. The integration cannot approve a decision or change case state. It is a local development CLI, **not** an authenticated, tenant-aware service.

## Run locally

From the repository root, with PostgreSQL running and the schema and synthetic seed data applied:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements-ai.txt
Copy-Item .env.example .env
```

Edit `.env` locally with your own API key and the database URL. Do not commit `.env`, real keys, or customer data. A ChatGPT subscription is not an API balance; check API billing and usage limits before running. Each run makes a billable API call, so avoid repeated requests.

```powershell
.venv\Scripts\python -m pytest tests/test_model_output.py -q
.venv\Scripts\python -m scripts.run_triage --evidence evaluations/model-run-02.json
```

The model call occurs only in the second command. The first recorded run is stored in `evaluations/model-run-01.json`, which is also the script's default evidence path. The script refuses to overwrite an existing evidence file and stops before calling the model, so each new experiment needs its own `--evidence` path. Never hand-author model output or mark a run as passed before observing it.

## Verify database state

```powershell
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -c "SELECT c.title, c.status, c.version, s.proposed_category, s.proposed_urgency, s.model_id, s.prompt_version FROM ai_suggestion s JOIN case_record c ON c.id = s.case_id ORDER BY s.created_at DESC LIMIT 5;"
```

After a successful run, a new suggestion row appears and the source case keeps its existing status and version. The evidence file holds the actual model output, model identifier, prompt version, token usage, and elapsed time.

## Observed experiment

The first run is recorded in [`evaluations/model-run-01.json`](../evaluations/model-run-01.json). All values below are taken from that file.

| Item | Recorded value |
|---|---|
| Recorded at (UTC) | 2026-11-13 11:31:31 |
| Provider | OpenAI |
| Requested model | `gpt-4o-mini` |
| Actual model | `gpt-4o-mini-2024-07-18` |
| Prompt version | `triage-v1` |
| Response status | `completed` |
| Structured-output validation | passed |
| Elapsed model request/parse time | 6512.03 ms |
| Input tokens | 272 |
| Output tokens | 44 |
| Total tokens | 316 |
| Source case state | `NEW`, version `1` |
| Case state after persistence | `NEW`, version `1` |
| Suggestion stored in | `ai_suggestion` |
| Environment | Windows, Python 3.12.6, OpenAI SDK 2.54.0 |

The PostgreSQL server version was not recorded by the script for this run.

**Input:** the synthetic `northwind` case "Employees cannot connect to VPN", described as "Multiple users report VPN connection failures."

**Validated model output:**

| Field | Value |
|---|---|
| `category` | `network` |
| `urgency` | `high` |
| `reason` | "Multiple users are experiencing VPN connection failures, indicating a critical network issue." |
| `suggested_next_step` | "Investigate the VPN server's status and connectivity logs." |

### Interpretation

- **One synthetic-case experiment.** This run demonstrates model invocation, structured-output validation, and suggestion-only persistence. It does not measure model accuracy.
- **Latency is a single observation.** The 6512.03 ms figure covers the model request and SDK parsing for one call. It is not an SLO, a percentile, or a statistically meaningful performance baseline.
- **Schema-valid is not the same as correct.** The output satisfied the contract, but that does not make it semantically right. The model categorized the VPN problem as `network`, selected `high` urgency, and described the issue as critical. Those are the model's actual outputs, not verified service-severity determinations; the case description does not establish how critical the outage is.
- **Recommendation, not action.** The suggestion was stored for human review. The case remained `NEW` at version `1`; accepting the suggestion and changing case state remain separate, authorized operations (see [architecture context](architecture-context.md), DEC-002 and DEC-003).

## Limitations

- OpenAI-hosted inference is used for this first integration; a local model could be substituted later without changing the business authority boundary.
- The script is not an API server and does not provide tenant authorization, reviewer approval, retries, human workflow, or production credential management.
- A response satisfying the schema can still be factually wrong or propose a poor next step. Model quality requires a separate evaluation against labeled cases.
- The category and urgency vocabularies are initial working values, enforced by the application rather than by database constraints.
- A transient provider or network failure ends the run; there is no retry.
- The local CLI is intended to run on fictional or synthetic inputs only.
