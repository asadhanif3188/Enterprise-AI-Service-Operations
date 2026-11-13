# Controlled Case Operations — PostgreSQL Validation Results

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Scope:** Local application-controlled case operations, forward-only state transitions, expected-version checks, and transactional status-event recording  
**Observed result:** **8 selected database-backed tests passed; 8 tests deselected**  
**Execution date/time:** Not recorded in the supplied pytest output  
**Source evidence:** `case-operations-test-output.txt`, generated from a local verbose pytest run

## Test environment

| Item | Observed or documented information |
|---|---|
| Host platform | Windows (`win32`) |
| Python | 3.12.6 |
| pytest | 9.2.0 |
| pluggy | 1.7.0 |
| pytest plugin | `anyio` 4.16.0 |
| Test configuration | `pytest.ini` |
| Executed test module | `tests/test_case_service.py` |
| Selected tests | 8 database-backed cases |
| Deselected tests | 8 offline transition-policy parameter cases |
| PostgreSQL target | Dedicated `eaiso_test` database required and checked by the test fixture in the accompanying test implementation |
| PostgreSQL server version | Not captured in the attached output |
| Schema prerequisites | Initial schema (`db/001_init.sql`) and status-event migration (`db/002_case_status_events.sql`) required by the test implementation |
| External model calls | None required by these tests |

The attached terminal output confirms that the eight database-backed tests executed and passed. It does not directly display the database connection string, the database migration output, or a separate offline-test execution. The test implementation includes guards for the dedicated `eaiso_test` database and checks for the presence of `case_status_event` before its database cases run.

## Reproduction

From the repository root, in **Windows Command Prompt** (`cmd.exe`), with the dedicated test database initialized and both migrations applied:

```cmd
set "TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_test"
.\.venv\Scripts\python.exe -m pytest tests/test_case_service.py -m db -v
set "TEST_DATABASE_URL="
```

To capture output to a local file instead of displaying it only in the terminal:

```cmd
set "TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_test"
.\.venv\Scripts\python.exe -m pytest tests/test_case_service.py -m db -v > case-operations-test-output.txt 2>&1
set "TEST_DATABASE_URL="
```

These commands are reproduction instructions. The development-only password shown here must not be reused in shared or deployed environments. **Do not run database-mutating tests against `eaiso_dev` or any non-test database.** The migration should be applied only once per database initialization.

## Observed test results

The eight executed cases reported the following outcomes:

| Test | Behavior asserted in the accompanying test implementation | Observed result |
|---|---|---|
| `test_create_and_read_case` | Create a case at `NEW`, version `1`, retrieve it within the supplied organization scope, and create no status event for initial creation | PASS |
| `test_entire_forward_lifecycle_and_event_history` | Move `NEW` → `IN_REVIEW` → `RESOLVED` → `CLOSED`; increment version from `1` to `4`; record the corresponding three versioned events | PASS |
| `test_skip_transition_rejected_without_write` | Reject `NEW` → `RESOLVED`; preserve the case version and avoid creating a transition event | PASS |
| `test_stale_version_rejected_without_second_event` | Reject a second transition using an outdated expected version; leave one successful event and the resulting version unchanged | PASS |
| `test_wrong_organization_scope_cannot_read_or_update` | Reject read and update attempts when the supplied organization ID does not match the case owner | PASS |
| `test_failed_event_insert_rolls_back_status_update` | Simulate an event-write failure and verify the status update and version increment are rolled back | PASS |
| `test_two_concurrent_transitions_only_one_succeeds` | Run two competing updates with the same expected version; verify one succeeds, one receives a stale-version error, and only one event is committed | PASS |
| `test_invalid_create_input_rejected` | Reject blank title and blank description values | PASS |

*The descriptions are based on the accompanying case-service test implementation. The PASS outcomes and test names are taken from the supplied terminal output.*

### Captured pytest output

```text
============================= test session starts =============================
platform win32 -- Python 3.12.6, pytest-9.2.0, pluggy-1.7.0 -- [local project virtual environment]
cachedir: .pytest_cache
rootdir: [local repository root]
configfile: pytest.ini
plugins: anyio-4.16.0
collecting ... collected 16 items / 8 deselected / 8 selected

tests/test_case_service.py::test_create_and_read_case PASSED             [ 12%]
tests/test_case_service.py::test_entire_forward_lifecycle_and_event_history PASSED [ 25%]
tests/test_case_service.py::test_skip_transition_rejected_without_write PASSED [ 37%]
tests/test_case_service.py::test_stale_version_rejected_without_second_event PASSED [ 50%]
tests/test_case_service.py::test_wrong_organization_scope_cannot_read_or_update PASSED [ 62%]
tests/test_case_service.py::test_failed_event_insert_rolls_back_status_update PASSED [ 75%]
tests/test_case_service.py::test_two_concurrent_transitions_only_one_succeeds PASSED [ 87%]
tests/test_case_service.py::test_invalid_create_input_rejected PASSED    [100%]

======================= 8 passed, 8 deselected in 2.35s =======================
```

The two machine-specific path values in the header have been shortened for publication; test names, statuses, package versions, and the summary are otherwise transcribed from the supplied log. The original local output file remains the underlying execution artifact.

## Interpretation

The selected integration tests support narrowly scoped claims about the **tested local case-service implementation**:

- The initial forward-only lifecycle can be executed through application-controlled operations, with a version increment and event for each successful transition.
- Invalid and stale transitions can be rejected without adding an event.
- The tested status update and transition-event insertion participate in one PostgreSQL transaction: the simulated event-insertion failure caused the update to roll back.
- The supplied organization ID restricts reads and transitions in the tested service methods. **This is query scoping, not authentication or authorization.**
- A two-thread competing-update test observed one successful transition and one version conflict. This does not prove behavior under arbitrary production concurrency or load.

**Eight deselected tests are not eight passes.** The attached output covers the database-marked tests only; the eight offline policy parameter cases must be executed and reported separately before claiming a combined 16/16 result.

## Remaining limitations

- **No verified caller identity or tenant authorization.** Organization IDs and `actor_label` are supplied by the caller. The recorded actor label is not proof of who performed an operation or that they were permitted to do so.
- **Incomplete business workflow.** Reviewer approvals, suggestion-to-decision binding, role restrictions, and reopening rules are not implemented by this increment.
- **Service-boundary enforcement only.** A database client with unrestricted write privileges can bypass the case service's transition policy. The event table is not a tamper-proof audit system.
- **Partial history.** The transition-event table records status changes performed through the service; it does not record creation, content edits, AI reviews, or every rejected decision.
- **Limited concurrency evidence.** One deterministic two-writer test passed. No extended concurrent load, deadlock, crash-recovery, or multi-process stress evidence is supplied.
- **Migration and cleanup evidence not independently captured here.** The attached log does not include migration output or a post-run manual inspection of test-row cleanup.

## Conclusion

**PostgreSQL-backed case-operations validation passed: 8 of 8 selected tests.** The evidence demonstrates the tested behavior of controlled forward transitions, version checks, transaction rollback, and organization-scoped operations in a local test environment. It does not establish an authorized end-to-end service workflow or production-level guarantees.

The offline policy-test result and any additional manual verification should be recorded as separate observed evidence before expanding the summary.
