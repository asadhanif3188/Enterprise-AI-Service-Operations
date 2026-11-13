# Controlled Case Operations — Initial Implementation

**Status:** Implementation and test suite prepared; database-backed results must be recorded after execution.

## Why this increment exists

The first database migration restricts the **set** of valid status values but does not restrict the **path** between states. A direct SQL update could change `NEW` to `CLOSED`, and neither the original `CHECK` constraint nor an AI output contract prevents this.

The local case service adds an initial explicit transition policy, expected-revision checks, and an event record written within the same PostgreSQL transaction as the state update.

## Initial transition policy

```text
NEW -> IN_REVIEW -> RESOLVED -> CLOSED
```

No reopening, skipping states, or no-op transitions are allowed in this initial policy. These are intentionally narrow **development rules** pending business decisions about role permissions, approval requirements, and reopening.

## Components

- `db/002_case_status_events.sql`: append-only-by-convention transition-event table; unique `(case_id, to_version)` and version-step checks. The uniqueness constraint creates the index used for version-ordered case history queries; no duplicate secondary index is created. The database does **not** prevent privileged users from editing or deleting these rows. Do not describe it as tamper-proof auditing.
- `src/case_service.py`: `create_case`, `get_case`, and `transition_case`; PostgreSQL row locking and expected-version checking; state update and event insertion in one transaction.
- `tests/test_case_service.py`: offline transition-policy tests and dedicated `eaiso_test` integration tests.

## Important trust boundaries

**No authentication or tenant authorization:** the supplied `organization_id` filters operations to a specific organization, but the caller controls that input. A user could supply a different organization's ID. This is not a public API and must not be exposed directly to untrusted callers.

**No verified actor identity or approval:** `actor_label` is a caller-supplied diagnostic label (for example `local-synthetic-operator`), not a verified user identity, an approval, or reliable attribution. Consequential case updates must be authorization-gated before a product workflow is claimed.

**AI remains non-authoritative:** `ai_suggestion` is not an input to the transition operation. A model cannot transition a case by returning an extra field or recommending closure. Application-controlled execution is still necessary.

**Version safety:** transitions use `SELECT ... FOR UPDATE`, check `expected_version`, and increment the version with the update. Other direct database writers can still bypass this service. The existing `scripts/run_triage.py` uses a separate check to reject stale suggestions.

**Partial history only:** this table records status transitions made through this service. It does not record case creation, text edits, suggestion reviews, permissions, or failed decisions. It is not a complete audit trail.

## Running tests against the dedicated database

Only the disposable `eaiso_test` database should be used for tests. If that database was initialized during the previous test increment, it already contains `db/001_init.sql`.

Apply the new migration **once** to `eaiso_test`:

```cmd
docker cp db/002_case_status_events.sql eaiso-postgres:/tmp/002_case_status_events.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_test -v ON_ERROR_STOP=1 -f /tmp/002_case_status_events.sql
```

Offline policy tests (no database):

```cmd
.\.venv\Scripts\python.exe -m pytest tests/test_case_service.py -m "not db" -q
```

Database-backed checks (Windows Command Prompt):

```cmd
set "TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_test"
.\.venv\Scripts\python.exe -m pytest tests/test_case_service.py -m db -q
set "TEST_DATABASE_URL="
```

Do not treat a skip as a pass. The tests create uniquely named synthetic records in `eaiso_test` and attempt cleanup after each test.

## Evidence requirements

After running tests, record **observed**, not expected, output under `tests/case-operations-results.md`. Include migration output, selected/deselected/failed counts, environment, and known limitations. Link the report from the README only after it exists. Do not alter previous model-run or failure-test evidence.

## Remaining work

Authenticated identity, role-based and organization-based authorization, reviewed and approved decisions, a full audit trail, and safe exposure through an application API are separate capabilities. This increment does not deliver an authorized end-to-end workflow.
