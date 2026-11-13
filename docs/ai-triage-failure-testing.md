# AI Triage Failure-Path Validation

**Status:** Initial failure-path validation completed. All 18 selected tests passed: 14 offline tests and 4 PostgreSQL-backed tests. See the [validation results](../tests/ai-triage-failure-results.md).

## Purpose

The initial AI triage CLI demonstrates a real-model, suggestion-only write path. This test increment probes what happens when the model cannot produce usable output, dependencies fail, or case data changes between inference and persistence.

The tests exercise the existing [`src/model_adapter.py`](../src/model_adapter.py) and [`scripts/run_triage.py`](../scripts/run_triage.py). They do not make paid model calls. They use injected fake provider responses and, for persistence tests, a separate PostgreSQL database.

## Tests and boundaries

| Scenario | Test mechanism | Expected behavior |
|---|---|---|
| Missing output field, invalid urgency, blank next step, unexpected approval/action fields | Pydantic validation with synthetic objects | Invalid responses are rejected |
| Provider timeout | In-memory SDK stub; verify zero retries configured | Exception propagates; no recommendation produced |
| Model refusal or incomplete response | In-memory SDK response stub | No usable recommendation returned |
| CLI timeout or connection error | Injected failure before persistence | No call to suggestion persistence; no evidence file written |
| Existing evidence file | Supply a pre-existing output path | Runner stops before DB read or model invocation; old file unchanged |
| Valid suggestion | Dedicated test database | A suggestion is inserted; case status/version remain unchanged |
| Stale case version or changed case status | Modify only a synthetic case in the test database before persistence | No suggestion inserted |
| Database constraint rejection | Attempt an insert with an invalid `model_id` in the test database | Transaction fails; no suggestion committed; case unchanged |

The original offline contract checks remain in [`tests/test_model_output.py`](../tests/test_model_output.py). The additional tests live in [`tests/test_triage_failures.py`](../tests/test_triage_failures.py).

## Run offline checks

From the repository root, using the Python virtual environment described in the README:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_model_output.py tests/test_triage_failures.py -m "not db" -q
```

Offline checks never contact an external provider or write to PostgreSQL. They use deterministic test doubles, so they cannot establish real provider uptime, actual network timeouts, or backend-side refusal rates.

## Set up a dedicated test database

**Do not set `TEST_DATABASE_URL` to the development database.** The database-backed tests create, update, and remove synthetic rows. The suite requires the database name to be exactly `eaiso_test` and verifies this before running those tests.

Start the existing local PostgreSQL container if it is stopped:

```powershell
docker start eaiso-postgres
```

Create a new, separate database **once**. The command will fail with "already exists" if it was created previously; that is not a reason to delete an existing database.

```powershell
docker exec eaiso-postgres createdb -U eaiso_dev eaiso_test
```

Apply the initial schema to this *new* database, not the development database:

```powershell
docker cp db/001_init.sql eaiso-postgres:/tmp/001_init.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_test -v ON_ERROR_STOP=1 -f /tmp/001_init.sql
```

The tests create their own fictional organizations and cases, so the seed script is **not** required in `eaiso_test`. Do not rerun the migration if the test schema is already initialized.

Set the test-only connection URL for the current terminal session.

PowerShell:

```powershell
$env:TEST_DATABASE_URL = "postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_test"
```

Windows Command Prompt (`cmd.exe`):

```cmd
set "TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_test"
```

Execute the database-backed tests (the same command works in both shells):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_triage_failures.py -m db -q
```

When finished, clear the variable from the current session.

PowerShell:

```powershell
Remove-Item Env:TEST_DATABASE_URL
```

Windows Command Prompt:

```cmd
set "TEST_DATABASE_URL="
```

The tests clean up their probe rows using a fixture. Keep the test database for future regression runs, or review its contents before dropping it deliberately. Never remove the development database or its Docker volume to clean up these tests.

## Evidence and interpretation

The observed results of the first execution are recorded in [`tests/ai-triage-failure-results.md`](../tests/ai-triage-failure-results.md): the commands used, the actual console output, pass and deselection counts, and the database outcomes asserted by each PostgreSQL-backed test. The PostgreSQL server version and Python/pytest package versions were not captured for that run. When the suite is rerun, record the new output rather than reusing these results; a skipped database test is **not** a successful database validation.

These passing tests support narrowly scoped claims about output-contract rejection, exception propagation, stale-state checks, and rollback on a constraint error. They do **not** establish tenant isolation, safe approval execution, production fault tolerance, end-to-end retry semantics, semantic accuracy, or race-free concurrency under all workloads.

**Caveat:** The case version check assumes case-writing operations correctly increment `case_record.version`. The current schema does not enforce this automatically. This suite probes mismatches that are already visible in stored state; it does not implement a full authorized case-update path.

## Known boundaries not yet covered

- **Persistence and evidence are separate writes.** The CLI commits the suggestion row before writing the JSON evidence file. If the local evidence write fails, the suggestion may be committed even though the command reports an exception and no evidence file is produced. This suite does not prove atomicity between PostgreSQL and the file system. Before supporting reliable background processing, define a durable execution record or a recovery/reconciliation strategy.
- **Revision discipline is not enforced by the current schema.** These tests deliberately update `version` or `status` to provoke a mismatch. A future authorized case-update operation must increment versions atomically for stale checks to be dependable.
- **No live provider fault injection or concurrency stress.** Provider errors are simulated offline, and each database test operates on one synthetic case. There is no real network chaos test or race/load test.
