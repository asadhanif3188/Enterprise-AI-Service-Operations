# Centralized Case Authorization — Validation Results

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Scope:**
- The permission policy [`src/case_authorization.py`](../src/case_authorization.py).
- The authorized-case-operations layer [`src/authorized_case_operations.py`](../src/authorized_case_operations.py).
- The structural no-bypass check [`scripts/check_case_access_boundary.py`](../scripts/check_case_access_boundary.py).
- Design summary: [Authentication sequence §7.1](../docs/authentication-authorization-sequence.md#71-implemented-authorized-case-operations).

**Governing decision:** [ADR-0005](../docs/adr/0005-server-verified-identity-and-tenant-authorization.md) (Accepted), using the [ADR-0006](../docs/adr/0006-actor-affiliation-and-role-grant-eligibility.md) grants and the local credential resolver  
**Observed result:** **76 of 76 tests passed, with 0 failed and 0 skipped:**
- 67 authorization tests (38 offline, 29 PostgreSQL-backed);
- 9 structural-check tests (offline).

The full repository suite (**255 tests**: 144 offline, 111 PostgreSQL-backed) passed in three consecutive runs, and the 11 schema-integrity SQL checks passed.  
**Execution date:** 2026-11-15

This validates a **Python authorization boundary**, not an HTTP API. No HTTP route exists, no HTTP test vector has been executed, and the statuses below (`401`, `403`, `404`, `500`) are the ADR-0005 equivalents carried by typed exceptions.

## Test environment

| Item | Observed |
|---|---|
| Host platform | Windows (`win32`), Docker Desktop |
| Python / pytest / psycopg | 3.12.6 / 9.2.0 / 3.3.9 |
| PostgreSQL | 16.14, official `postgres:16` image, in the same disposable verification container used for the [identity](identity-grant-integrity-results.md) and [credential](credential-verification-results.md) results (`127.0.0.1:55432`), not the developer's `eaiso-postgres` container |
| Test database | `eaiso_test` with `db/001`, `db/002`, the seed data, and `db/003` applied; default `READ COMMITTED`, `deadlock_timeout = 1s` |
| Test data | Each database test creates two fictional customer organizations, six fictional actors with ADR-0006-eligible grants, credentials generated at test time in a temporary store, and synthetic cases, then deletes them |

The database fixture requires the configured and the connected database to be `eaiso_test`.

## Commands executed

With `TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:55432/eaiso_test`:

```text
python -m pytest tests/test_case_authorization.py -m "not db" -q   # 38 passed, 29 deselected
python -m pytest tests/test_case_authorization.py -m db -v         # 29 passed
python -m pytest tests/test_case_access_boundary.py -q              # 9 passed
python -m scripts.check_case_access_boundary                       # 0 violation(s)
python -m pytest tests -m "not db" -q                              # 144 passed, 111 deselected
python -m pytest tests -m db -q                                    # 111 passed, 144 deselected
python -m pytest tests -q                                          # 255 passed (three runs)
psql -d eaiso_test -v ON_ERROR_STOP=1 -f tests/test_schema_integrity.sql   # ALL 11 CHECKS PASSED
```

Per module in the full run:

| Module | Tests passed |
|---|---|
| Case authorization | 67 |
| Structural boundary | 9 |
| Credentials | 58 |
| Identity and grants | 87 |
| Case service | 16 |
| AI triage failures | 14 |
| Model output | 4 |

After the runs, `eaiso_test` held no `actor` or `organization_membership` rows, and only the two seeded organizations and cases.

## Fictional test world

| Actor | Affiliation | Grants |
|---|---|---|
| `engineer_a` | provider | `service_engineer` in A |
| `manager_a` | provider | `service_manager` in A |
| `requester_a` | customer of A | `requester` in A |
| `engineer_ab` | provider | `service_engineer` in A and in B |
| `engineer_b` | provider | `service_engineer` in B |
| `platform_admin` | provider | none |

## Permission matrix (policy, offline)

`test_permission_matrix_offline` (20 cases) and `test_policy_recognizes_exactly_three_actions_offline`:

| Role | `case:create` | `case:read` | `case:list` |
|---|---|---|---|
| `service_engineer` | Allow | Allow | Allow |
| `service_manager` | Allow | Allow | Allow |
| `requester` | Allow | Deny | Deny |
| `platform_administrator`, unknown, empty, or `None` role | Deny | Deny | Deny |

Unknown actions (`case:update`, `case:transition`, `membership:grant`, empty, `None`) are denied, and `roles_permitting` raises `UnsupportedAction` for them. The SQL role predicates are derived from this one mapping.

## Observed authorization behavior (PostgreSQL-backed)

Wherever a write is denied, the test compares a snapshot of every case row in the test organizations, and the global `case_record` count, before and after.

| Scenario | Observed | Test |
|---|---|---|
| Engineer and manager create, read, and list in A | Create returns the persisted representation (`NEW`, version 1, equal to the stored row); read returns the same; list returns it without `description` | `test_engineer_and_manager_create_read_and_list_in_authorized_customer` ×2 |
| Requester | Creates in A; then gets `CaseNotFound` reading its own case, and `OrganizationAccessDenied` listing A and creating in B; no further writes | `test_requester_creates_but_cannot_read_or_list` |
| Engineer of A against B | Create, read, and list in B denied (403, 404, 403); state unchanged | `test_engineer_of_a_cannot_create_read_or_list_b` |
| Engineer with grants in A and B | Each list returns only that customer's case; deactivating the B grant denies B and leaves A working | `test_engineer_with_grants_in_a_and_b_accesses_each_separately` |
| No membership; platform administrator with no grants | All three operations denied in A, and the administrator is also denied listing B; state unchanged | `test_no_membership_and_platform_administrator_have_no_access` |
| Inactive membership | Create 403, read 404, list 403; state unchanged | `test_inactive_membership_denies` |
| Inactive actor | Refused by the resolver (`InactiveActor`, 403). With a stale identity (deactivated after resolution), the layer's own checks also return `InactiveActor` for all three; state unchanged | `test_inactive_actor_denies_via_resolver_and_via_layer_check` |
| Stale identity for a nonexistent actor | `Unauthenticated("actor_not_found")` for all three | `test_stale_identity_for_deleted_actor_is_unauthenticated` |
| Unknown role or action | The database `CHECK` rejects `platform_administrator` as a role; the layer exposes only `create_case`, `get_case`, `list_cases` | `test_unknown_role_cannot_exist_and_unknown_action_has_no_entry_point` |
| Nonexistent organization | Create and list raise exactly the same `OrganizationAccessDenied` (type, message, args, reason, status) as a forbidden existing organization; no write | `test_nonexistent_organization_denied_like_forbidden_one_without_writes` |
| Missing, foreign, and unreadable cases | Four reads (missing UUID, B's case, requester's own case, administrator) give identical `CaseNotFound` (type, `str`, `repr`, args, reason, status) | `test_missing_foreign_and_unreadable_cases_are_indistinguishable` |
| Spoofed identity and tenant | A constructed `VerifiedActor`, a raw actor UUID (string or object), or a display name passed as the credential gives `Unauthenticated`; extra `actor_id`/`authorized` arguments raise `TypeError`; B's engineer selecting A is denied; state unchanged | `test_spoofed_actor_and_tenant_identifiers_never_override_credential_identity` |
| Credential revocation and rotation | After rotation the new credential reads; the old one gets `Unauthenticated("revoked")` for create, read, and list; no write | `test_credential_revocation_denies_new_attempts_and_rotation_keeps_access` |
| Database outage | Unreachable layer database: `CaseOperationUnavailable("database")` for all three, no exception chaining; unreachable resolver database: `AuthenticationUnavailable`; no write | `test_database_outage_fails_closed_without_writes` |
| List bounds | Ordered by `(created_at, id)`; `limit=2` returns the first two with `has_more = true`; `limit=100` returns all with `has_more = false`; only the selected organization; an authorized organization with no cases returns `{items: [], has_more: false}` | `test_list_is_bounded_ordered_and_single_tenant` |
| Unkeyed create | Two identical requests create two cases | `test_unkeyed_create_is_not_idempotent` |
| Logging | No credential, title, or description in any log record; decision categories present | `test_logs_contain_no_credentials_or_case_text` |

Offline checks also confirm:

- **Input validation, before any database connection** (15 cases, using an unreachable DSN). The layer rejects malformed or non-canonical UUIDs (braces, `urn:uuid:`, unhyphenated), blank text, NUL characters, text over 10,000 characters, and out-of-range or non-integer limits.
- **Order of checks:** authentication precedes input validation.
- **Entry-point signatures:** each entry point accepts only the credential and request fields, with no actor, decision, or scope parameter.

## Revocation interleavings for create (real sessions)

A session counts as waiting only when `pg_blocking_pids()` names the expected holder; the test fails if an operation completes without waiting.

| # | Interleaving | Observed | Final database state |
|---|---|---|---|
| 1a | Membership deactivation **uncommitted** → create waits on the membership `FOR SHARE` → revocation commits | Create re-evaluates and raises `OrganizationAccessDenied` | No case; snapshot unchanged |
| 1b | Actor deactivation uncommitted → create (after the resolver's unlocked check succeeded) waits on the actor `FOR SHARE` → commit | Create raises `InactiveActor` | No case; snapshot unchanged |
| 1c | Role change (engineer → manager) uncommitted → create waits → commit | Create succeeds under the new role (every ADR-0006-eligible role keeps `case:create`, so this proves ordering, not denial) | One case; grant is `service_manager` |
| 2a | Create holds both locks (held at its `INSERT` by a `SHARE` lock on `case_record`) → membership deactivation is **blocked by the create's backend** → create commits | Revocation completes only after the create commits; the next create is denied | Exactly one new case (`NEW`, in A); membership inactive |
| 2b | Same, with actor deactivation | Same ordering; the next create gets `InactiveActor` | One new case; actor inactive |
| 2c | Same, with a role change | Same ordering | One new case; grant changed after the commit |
| 3 | Lock timeout (300 ms) behind an uncommitted membership or actor deactivation | `CaseOperationUnavailable("lock_timeout")`, **not** a denial, after ~0.3 s | No case |
| 4 | Create's backend terminated while waiting at its `INSERT` | `CaseOperationUnavailable` | No case |
| 5 | Failure raised from `COMMIT` (simulated on the create connection only) | `CaseCreationOutcomeUnknown` (500), never success or "rolled back"; no exception chaining | No case. In the simulation `COMMIT` was never sent; the layer cannot know that and correctly reports the outcome as unknown |

**Read-side snapshot behavior** (`test_reads_do_not_wait_for_uncommitted_revocation_and_deny_after_commit`):

- While a membership deactivation was uncommitted, `get_case` and `list_cases` completed without waiting and returned the case from the committed state.
- After the commit, the read gave `CaseNotFound` and the list gave `OrganizationAccessDenied`.
- This shows the accepted limitation: revocation is not instantaneous for in-flight reads.

## Structural no-bypass check

`python -m scripts.check_case_access_boundary` reports **0 violations** on the repository, and the check runs inside the test suite (`test_repository_respects_the_boundary`). The permitted route example passes: it imports FastAPI, Pydantic, the authorized layer, the policy outcomes, the credential module, a sibling route module, and the standard library.

Forbidden examples fail as intended:

| Example | Violations reported |
|---|---|
| Route importing `psycopg`, `src.case_service` (three forms), `src.identity_provisioning`, `importlib`; calling `import_module`, `__import__`, `eval`; embedding `case_record` SQL (also in an f-string) | 6 forbidden imports, 3 dynamic-code calls, 2 case-SQL literals |
| Route importing `psycopg_pool`, `pg8000`, `runpy`, `sqlite3`, `builtins`, `src.identity_provisioning`, a wrapper module; using `ops._connect`, `ops._dsn`, `getattr(builtins, "__import__")`, `vars`, `__builtins__` | 7 forbidden imports, 2 private-attribute accesses, reflection flagged |
| Relative imports `from ... import case_service` in `src/api/routes/` | Both resolved to `src.case_service` and flagged |
| FastAPI, Flask, or `http.server` imported outside `src/api/` | `web-framework-outside-route-package` for each |
| A non-route module importing `src.case_service`; a non-allowlisted module with protected-table SQL | Flagged; allowlisted modules and tests are not |
| A route module claiming SQL-allowlist exemption; unparseable or non-UTF-8 files | Flagged |

**What the check does not protect against:**
- privileged or direct SQL clients;
- code outside `src/` and `scripts/`, and tests;
- SQL assembled at runtime or hidden behind views with other names;
- allowlisted modules misusing their access;
- reviewers widening the allowlists.

It complements code review. It is not a database control: row-level security and least-privilege roles are not implemented.

## Negative controls

Each mutation of the layer or policy was applied alone, the 67 authorization tests were run, and the file was restored byte-for-byte afterwards (never committed):

| Mutation | Tests failed |
|---|---|
| Create does not lock the actor row | 3 |
| Create locks the membership `FOR KEY SHARE` instead of `FOR SHARE` | 5 |
| Create ignores an inactive membership | 3 |
| Read drops the role predicate | 2 |
| Read drops the ownership join (any membership of the actor) | 5 |
| List drops the organization filter | 5 |
| Policy lets requesters read and list | 5 |
| Commit failure reported as an ordinary failure | 1 |

## Independent review

A read-only security review found **no authorization bypass**. It raised two MEDIUM findings, both fixed before the results above were recorded:

- **Route detection was path-only.** A route module outside `src/api/` would have escaped the strict rules. Web-framework imports outside `src/api/` are now violations.
- **Route rules were a deny-list.** Unlisted drivers such as `psycopg_pool`, reflection, and private attributes such as `ops._connect` were not caught. Route imports are now allowlist-only, and reflection and private-attribute access are flagged.

It also raised LOW findings, now fixed:

- NUL characters and unbounded text reached the locked transaction; both are now rejected before any lock.
- Non-canonical UUID spellings were accepted; only the canonical form is.
- A non-UTF-8 file crashed the checker; it is now reported as a violation.
- Two timing bounds were tight; both are widened.

## Limitations

- **No HTTP boundary.** No route, HTTP response, header handling, or HTTP tenant-isolation test exists; ADR-0005's HTTP vectors remain unexecuted.
- **Application-layer enforcement.** A superuser, a direct SQL client, `src/case_service.py` (still local and unauthenticated), and any allowlisted module can bypass the layer. There is no row-level security and no least-privilege database role.
- **Not instantaneous revocation.** In-flight reads complete under their snapshot. Writes follow the accepted lock ordering.
- **Role changes cannot deny create** under ADR-0006 eligibility; the role-change tests demonstrate ordering only.
- **Two database round trips for identity.** Credential resolution and the operation use separate connections. The layer re-checks the actor inside its own statement or transaction, so a deactivation between the two is still caught.
- **Deferred:**
  - list cursors (`has_more` only);
  - keyed idempotent creation (ADR-0004);
  - requester status visibility;
  - audit attribution of the verified actor on cases;
  - correlation IDs in logs.
- **One environment.** One Windows machine, one PostgreSQL 16.14 container, superuser test role, small synthetic data; no load testing.

## Reproduction

With the README's local container on port 5432 and migrations 001–003 applied to `eaiso_test`, from the repository root in **Windows Command Prompt**:

```cmd
.\.venv\Scripts\python.exe -m scripts.check_case_access_boundary
.\.venv\Scripts\python.exe -m pytest tests/test_case_authorization.py tests/test_case_access_boundary.py -m "not db" -v
set "TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_test"
.\.venv\Scripts\python.exe -m pytest tests/test_case_authorization.py -m db -v
.\.venv\Scripts\python.exe -m pytest tests -q
set "TEST_DATABASE_URL="
```

Do not treat a skip as a pass. Never point `TEST_DATABASE_URL` at `eaiso_dev` or any non-test database. The development password shown is for an isolated local container only.
