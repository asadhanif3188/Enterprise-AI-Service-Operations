# AI Triage Failure-Path Validation Results

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Run date:** 2026-11-13 (local development; exact execution timestamp not captured)  
**Result:** Passed — 18 selected test cases executed successfully across two test commands  
**Scope:** Deterministic AI-output and provider-failure simulations, plus PostgreSQL-backed persistence and stale-case checks

## Test environment

| Item | Recorded information |
|---|---|
| Execution host | Windows, Command Prompt (`cmd.exe`) |
| Python environment | Project virtual environment (`.venv`) |
| Test runner | `pytest` (version not captured in terminal output) |
| Database service | PostgreSQL in Docker container `eaiso-postgres` |
| Database used for integration checks | Dedicated `eaiso_test` database |
| Database role | `eaiso_dev` (local development account) |
| Schema | `db/001_init.sql` applied successfully to the test database |
| Tests | `tests/test_model_output.py` and `tests/test_triage_failures.py` |
| External model traffic | None from these tests; provider conditions are simulated |

The PostgreSQL server version and Python/pytest package versions were not captured in the supplied outputs. The separate real-model experiment is documented in [`evaluations/model-run-01.json`](../evaluations/model-run-01.json).

## Execution and observed output

### 1. Offline output-contract and dependency-failure tests

From the repository root:

```cmd
.\.venv\Scripts\python.exe -m pytest tests/test_model_output.py tests/test_triage_failures.py -m "not db" -q
```

Observed:

```text
..............                                                                                         [100%]
14 passed, 4 deselected in 2.85s
```

The 14 executed checks cover output-contract validation, rejection of unexpected fields, simulated provider timeout/refusal/incomplete responses, error propagation without an automatic retry in the adapter, CLI handling of simulated dependency failures, and refusal to overwrite an existing evidence path. `4 deselected` means the PostgreSQL-marked tests were deliberately excluded from this command, **not** that they passed or failed.

### 2. Dedicated test database initialization

The `eaiso_test` database was created using the existing Docker container. The schema was copied into the container and applied:

```cmd
docker exec eaiso-postgres createdb -U eaiso_dev eaiso_test
docker cp db/001_init.sql eaiso-postgres:/tmp/001_init.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_test -v ON_ERROR_STOP=1 -f /tmp/001_init.sql
```

Observed migration output:

```text
BEGIN
CREATE TABLE
CREATE TABLE
CREATE TABLE
COMMIT
```

The tests create their own temporary synthetic cases; the development seed script was not required for the dedicated test database.

### 3. PostgreSQL-backed tests

Commands used in **Windows Command Prompt** (not PowerShell):

```cmd
set "TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_test"
.\.venv\Scripts\python.exe -m pytest tests/test_triage_failures.py -m db -q
set "TEST_DATABASE_URL="
```

The environment variable was checked with `echo %TEST_DATABASE_URL%` before testing and cleared afterward. The sample password is for this local-only Docker environment; do not reuse it in shared or deployed systems.

Observed test output:

```text
....                                                                                                   [100%]
4 passed, 10 deselected in 0.98s
```

The four database-backed tests passed their assertions for the following conditions:

| Test | Asserted behavior | Result |
|---|---|---|
| Valid suggestion persistence | Stores one suggestion and leaves case status `NEW` and version `1` unchanged | PASS |
| Stale case revision | Rejects a suggestion after the case version changes; no suggestion row inserted | PASS |
| Changed case status | Rejects a suggestion after the case status changes; no suggestion row inserted | PASS |
| Database constraint rejection | Invalid blank `model_id` raises a PostgreSQL constraint error; no suggestion row committed and case state unchanged | PASS |

`10 deselected` means only tests marked `db` were selected for this command. The test fixture uses a unique synthetic organization and case for each test and attempts to remove the probe rows afterward; this is based on the test implementation, not a separately captured manual post-run table inspection.

## Combined result and interpretation

| Group | Selected and passed | Excluded by marker |
|---|---:|---:|
| Offline output-contract and failure simulations | 14 | 4 |
| PostgreSQL-backed persistence and failure checks | 4 | 10 |
| **Total unique tests passed** | **18** | — |

These results support **narrowly scoped** assertions about local output validation, error propagation, protecting an existing evidence file, rejecting an already-visible stale case state, and rollback after a database constraint failure. They do **not** establish live provider reliability, production retry behavior, tenant authorization, end-to-end workflow safety, semantic recommendation accuracy, or race-free concurrency in a deployed service.

### Remaining engineering limitations

- Case version checks assume legitimate updates consistently increment `case_record.version`. The initial database schema does not automatically enforce that discipline.
- PostgreSQL suggestion persistence and writing a JSON evidence file are separate operations. A failed evidence write can occur **after** a suggestion has been committed; atomicity or reconciliation across these two operations is not yet implemented.
- Provider timeout, connection failure, refusal, and incomplete-response scenarios were simulated with deterministic test doubles. They are not live provider fault-injection measurements.
- The suite does not test authorization or tenant isolation, reviewer approval and action execution, duplicate-safe retries, concurrent writer race conditions under load, or recovery after a process crash.

## Conclusion

**The initial AI triage failure-path test increment passed all 18 selected checks.** The observed result consists of 14 offline checks and 4 dedicated-database checks. Further end-to-end and operational guarantees require additional implementation and evidence.
