# Actor Identity and Role-Grant Integrity — Validation Results

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Scope:** `db/003_actor_identity_and_grants.sql` (actor affiliation, customer access grants, ADR-0006 eligibility guards), the trusted local provisioning module `src/identity_provisioning.py`, and the local provisioning command `scripts/provision_dev_identities.py`  
**Governing decisions:** [ADR-0005](../docs/adr/0005-server-verified-identity-and-tenant-authorization.md) and [ADR-0006](../docs/adr/0006-actor-affiliation-and-role-grant-eligibility.md), both Accepted  
**Observed result:** **87 of 87 identity tests passed:** 61 PostgreSQL-backed tests (`-m db`) and 26 offline tests (`-m "not db"`), in separate runs, with **0 failed and 0 skipped**. The full repository suite (121 tests, including the existing case-service and AI-triage tests) passed in three consecutive runs, and the 11 schema-integrity SQL checks passed with the new migration applied.  
**Execution date:** 2026-11-15

This report records what was executed and observed. It does **not** establish authenticated requests, request-time tenant authorization, or production-grade access control; see [Limitations](#limitations).

## Test environment

| Item | Observed |
|---|---|
| Host platform | Windows (`win32`), Docker Desktop |
| Python / pytest / psycopg | 3.12.6 / 9.2.0 / 3.3.9 (libpq 18.0.4) |
| PostgreSQL | 16.14 (Debian 16.14-1.pgdg13+1), official `postgres:16` image |
| Database server | A **separate, disposable verification container** with a fresh volume, published on `127.0.0.1:55432`. The developer's usual `eaiso-postgres` container and its data volume were not used or modified. |
| Test database | `eaiso_test`, created empty, then `db/001_init.sql`, `db/002_case_status_events.sql`, `db/seed_dev.sql`, and `db/003_actor_identity_and_grants.sql` applied in that order with `ON_ERROR_STOP=1` |
| Server settings relevant to the tests | `default_transaction_isolation = read committed`, `deadlock_timeout = 1s` (defaults) |
| Test role | The container's `eaiso_dev` bootstrap role, which is a **superuser**. Two tests use superuser-only settings (see [Superuser-dependent tests](#superuser-dependent-tests)). |
| External model calls | None |

The test fixture refuses to run unless the configured database name **and** the connected database (`current_database()`) are both `eaiso_test`, and it fails if migration 003 has not been applied.

## Migration applied

`db/003_actor_identity_and_grants.sql` applied without error after migrations 001 and 002 and the seed data, creating: `actor`, `organization_membership`, the function `role_grant_is_eligible`, the trigger functions and triggers `organization_membership_role_eligible` and `actor_grants_remain_eligible`, and the view `organization_membership_eligibility_violation`. It inserts no rows; in particular, the provider (ABC) is not added to `organization`.

## Commands executed

PowerShell or Bash, from the repository root, with `TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:55432/eaiso_test`:

```text
python -m pytest tests/test_identity_grants.py -m "not db" -v      # 26 passed, 61 deselected
python -m pytest tests/test_identity_grants.py -m db -v -s         # 61 passed, 26 deselected
python -m pytest tests -q                                          # 121 passed (run three times)
python -m pytest tests/test_case_service.py -m db -q               # 8 passed
python -m pytest tests/test_triage_failures.py -m db -q            # 4 passed
psql -d eaiso_test -v ON_ERROR_STOP=1 -f tests/test_schema_integrity.sql   # ALL 11 CHECKS PASSED
```

After the runs, `eaiso_test` contained 0 `actor` rows and 0 `organization_membership` rows, and only the two seeded organizations and cases: the tests removed their own synthetic records.

## Observed results

### Schema (13 tests, all PASS)

| Behavior | Tests |
|---|---|
| Provider actor accepted; `id` is a database-generated version-4 UUID; `is_active` defaults to true; `created_at` set | `test_valid_provider_actor_has_generated_id_and_defaults` |
| Customer actor with a home organization accepted | `test_valid_customer_actor` |
| Unknown affiliation type, provider with a home organization, and customer without one are rejected by `CHECK` constraints; no row is written | `test_invalid_actor_affiliation_rejected` ×3 |
| Blank display name and missing affiliation rejected | `test_blank_display_name_and_missing_affiliation_rejected` |
| Nonexistent home organization rejected by `actor_home_organization_id_fkey` | `test_nonexistent_home_organization_rejected` |
| Second grant for the same actor and organization rejected by `organization_membership_pkey` (also as `ProvisioningConflict`) | `test_duplicate_membership_rejected` |
| Unknown roles, including `platform_administrator` and the empty string, rejected by `organization_membership_role_valid` | `test_unknown_membership_role_rejected` ×3 |
| Nonexistent actor or organization rejected by the grant's foreign keys; referenced organizations and actors cannot be deleted (`ON DELETE RESTRICT`) | `test_membership_foreign_keys_enforced` |
| Both guard triggers exist and are enabled; the migration contains no `INSERT` | `test_catalog_guards_present_and_enabled` |

### Eligibility (29 tests, all PASS)

Each row checks the **database guard** with direct SQL and, where applicable, the **provisioning path**, and asserts the persisted state afterwards.

| ADR-0006 rule | Observed | Tests |
|---|---|---|
| Provider engineer and manager grants | Allowed | `test_provider_engineer_and_manager_grants_allowed` ×2 |
| Provider requester grant, active or inactive | Rejected (`organization_membership_role_eligible`; `EligibilityError`); no row | `test_provider_requester_grant_rejected_even_if_inactive` ×2 |
| Customer requester grant in the home organization | Allowed | `test_customer_requester_in_home_organization_allowed` |
| Customer requester grant in another organization, active or inactive | Rejected; no row | `test_customer_requester_in_foreign_organization_rejected` ×2 |
| Customer engineer or manager grant, home or foreign, active or inactive | Rejected; no row | `test_customer_engineer_or_manager_grant_rejected` ×8 |
| One provider holding separate grants for two customers | Allowed; deactivating one leaves the other active | `test_provider_holds_separate_grants_in_two_customers` |
| Changing a grant's role or organization to an ineligible value, including a role change on an inactive grant | Rejected; grant unchanged | `test_update_to_ineligible_role_or_organization_rejected` |
| Inactive historical grant after an affiliation change | Row kept, inactive, not reported as a violation, not returned as an active grant | `test_inactive_historical_grant_remains_but_is_not_authoritative` |
| Reactivating an incompatible grant | Rejected (direct and provisioning); grant stays inactive | `test_reactivation_of_incompatible_grant_rejected` |
| Reactivating a compatible grant | Allowed; repeated deactivation and reactivation are no-ops | `test_reactivation_of_compatible_grant_allowed` |
| Affiliation change while an incompatible grant is active (provider to customer; customer home change; customer to provider) | Rejected (`actor_grants_remain_eligible`; `EligibilityError`); actor unchanged | `test_affiliation_change_with_incompatible_active_grants_rejected` ×2, `test_customer_home_change_with_active_requester_grant_rejected` |
| Affiliation change after deactivating incompatible grants, separately or with explicit `deactivate_incompatible=True` | Allowed; grants deactivated in `organization_id` order, never deleted or rewritten | `test_affiliation_change_after_deactivating_incompatible_grants_allowed` |
| Deactivation | Never blocked; a later role change on the inactive grant keeps it inactive | `test_deactivation_is_never_blocked_and_inactive_role_change_preserves_state` |
| NULL role | Reported by the `NOT NULL` constraint, not by the eligibility guard | `test_null_role_reported_by_not_null_constraint` |
| Guarded writes under `REPEATABLE READ` or `SERIALIZABLE` | Rejected (`object_not_in_prerequisite_state`); deactivation still allowed | `test_guarded_writes_require_read_committed` ×2 |
| Trigger bypass with `session_replication_role = replica` | The bypass **succeeds** (documented limitation); `organization_membership_eligibility_violation` reports the ineligible row; reactivation is rejected; deactivation clears it | `test_bypassed_triggers_are_detected_by_integrity_view` |

### Provisioning (3 tests, all PASS)

| Behavior | Test |
|---|---|
| Provisioning refuses to run inside a caller-managed transaction (where "success" would not mean committed); `lock_actor` outside a transaction is refused | `test_provisioning_refuses_caller_managed_transaction` |
| `provisioning_transaction()` applies several operations atomically, rolls back all of them on failure, rolls back a failed operation to its savepoint, and cannot be nested | `test_provisioning_transaction_groups_operations_atomically` |
| The fictional scenario (Ahmed and Fatima, customer requesters; Ali, provider engineer for A and B; Sara, provider manager for A and B; Bilal, provider with no grants) is created exactly; a rerun creates nothing and returns the same actor IDs; a drifted (deactivated) grant makes a rerun stop with `ProvisioningConflict` instead of reactivating it | `test_synthetic_scenario_provisioning_is_correct_and_rerunnable` |

### Concurrency (16 tests, all PASS)

Each scripted interleaving uses separate PostgreSQL sessions. A session counts as waiting only when `pg_blocking_pids()` names the expected lock holder; a test fails if the waiter finishes without waiting. Every wait is bounded (`lock_timeout`, `statement_timeout`, and future timeouts).

| Scenario | Path | Observed outcome |
|---|---|---|
| Uncommitted affiliation change (provider to customer) holds the actor; a grant is requested | Provisioning | Grant waits, then is rejected (`EligibilityError`); no grant |
| Uncommitted grant holds the actor; an affiliation change is requested | Provisioning | Change waits, then is rejected; actor unchanged, grant active |
| Uncommitted actor `UPDATE`; direct grant `INSERT` | Direct SQL (trigger only) | `INSERT` waits **inside the trigger** (`FOR SHARE`), then is rejected by `organization_membership_role_eligible` |
| Uncommitted direct grant `INSERT`; direct actor `UPDATE` | Direct SQL | `UPDATE` waits on the trigger's `FOR SHARE`, then its guard sees the committed grant and rejects it (`actor_grants_remain_eligible`) |
| Uncommitted affiliation change; grant reactivation | Provisioning and direct SQL | Reactivation waits, then is rejected; grant stays inactive |
| Uncommitted direct reactivation; direct affiliation change | Direct SQL | Change waits, then is rejected; grant active, actor unchanged |
| Uncommitted deactivation; role change of the same grant | Provisioning | Role change waits, then applies **without** reactivating the grant (no lost update) |
| In-flight request-time locks (actor, then membership, `FOR SHARE`, as in ADR-0005); revocation | Provisioning | Revocation waits until the in-flight transaction ends |
| Actor-first order | Provisioning vs. direct | While provisioning waits for the actor lock, the membership row is **not** locked (`FOR UPDATE NOWAIT` from a third session succeeds). A direct `UPDATE` waiting in its trigger **does** hold the membership row lock (`NOWAIT` fails): the lock-order inversion described in ADR-0006 |
| `FOR KEY SHARE` vs. `FOR SHARE` on the actor | Direct SQL | `FOR KEY SHARE` does **not** block an affiliation update; `FOR SHARE` does (update hits `lock_timeout`) |
| Direct writer bypasses actor-first order while provisioning holds the actor | Provisioning vs. direct | PostgreSQL detected a deadlock and aborted exactly one transaction. In every observed run the aborted one was the direct writer (`DeadlockDetected`), and provisioning succeeded |
| Same, with the direct session's `deadlock_timeout` raised so provisioning is the victim | Provisioning vs. direct | Provisioning raised `ProvisioningError("Deadlock detected; …not applied")`; its change was not applied; the direct writer then succeeded |
| Lock timeout while an in-flight transaction holds the actor | Provisioning | `ProvisioningError("Lock timeout; …")` after about 0.3 s; no grant |
| 25 barrier-started races: grant creation vs. affiliation change | Provisioning | Outcomes `{(grant ok, change rejected): 9, (grant rejected, change ok): 16}` |
| 25 barrier-started races: grant creation vs. affiliation change | Direct SQL | Outcomes `{(grant rejected, change ok): 23, (grant ok, change rejected): 2}` |

In every race, exactly one side committed, the persisted state matched the winner, and the integrity view was empty afterwards. Race outcome counts vary between runs; the scripted interleavings above are what deterministically cover both orders.

### Negative controls

To check that the concurrency tests can fail, the `-m db` suite was also run against deliberately weakened guards in `eaiso_test`, which was then rebuilt from the real migration before the results above were recorded:

| Mutation (not committed) | Result |
|---|---|
| Membership guard locks the actor `FOR KEY SHARE` instead of `FOR SHARE` | **8 failed**, 53 passed: the four direct-SQL interleavings, the actor-first order test, both deadlock tests, and the direct-SQL race |
| Actor-side trigger dropped | **9 failed**, 52 passed: the catalog check, the affiliation-change rejections, both isolation tests, two direct interleavings, and the direct-SQL race |

### Offline tests (26, all PASS)

The ADR-0006 matrix in Python (11 cases), the affiliation-shape mirror (3), the command's refusal of non-local or non-allowed targets including `hostaddr` and `service` redirection (6) and acceptance of a local `eaiso_test` URL (1), and the translation of deadlock, lock-timeout, statement-timeout, serialization, and other database errors into `ProvisioningError` (5).

### Regression

| Check | Result |
|---|---|
| `tests/test_case_service.py` (8 PostgreSQL-backed, 8 offline) | PASS, unchanged |
| `tests/test_triage_failures.py` and `tests/test_model_output.py` (offline and 4 PostgreSQL-backed) | PASS, unchanged |
| `tests/test_schema_integrity.sql` (11 checks; probe writes rolled back) | ALL 11 PASSED with migration 003 applied |
| `src/case_service.py`, migrations 001–002, seed data | Not modified |

### Local provisioning command

Run twice against `eaiso_dev` in the verification container, after migrations 001–003 and the seed:

- First run: created the five fictional actors and six grants (Ahmed requester in `northwind`; Fatima requester in `summit`; Ali engineer in both; Sara manager in both; Bilal none); 0 eligibility violations; exit code 0.
- Second run: created nothing, printed the same actor IDs; exit code 0.
- A URL naming the `postgres` database was refused with exit code 1.

### Superuser-dependent tests

`test_bypassed_triggers_are_detected_by_integrity_view` (uses `session_replication_role`) and `test_deadlock_victim_provisioning_reports_failure_and_writes_nothing` (raises `deadlock_timeout` for one session) skip, and must be reported as **skipped**, under a non-superuser test role. Both executed and passed here.

## Interpretation

Within the tested local environment:

- The schema enforces the actor affiliation shape, the role vocabulary, one grant per actor and customer, and referential integrity.
- Every ADR-0006 eligibility rule exercised above is enforced by the PostgreSQL guards for direct SQL, and by provisioning before it writes.
- In the tested interleavings and races, concurrent affiliation changes and grant creation or reactivation never both committed an invalid active grant, whether through provisioning or through direct SQL with triggers enabled.
- Deadlocks, lock timeouts, and isolation-level violations surfaced as failures, never as successful grants.

This is evidence for the scenarios listed, not a proof of race freedom under every possible workload, configuration, or privilege level.

## Limitations

- **No authentication or request-time authorization.** Nothing resolves a credential to an actor, and `src/case_service.py` does not read these tables. Case operations remain unprotected; ADR-0005 enforcement is future work.
- **Privileged bypass.** A superuser, a table owner who disables triggers, or a session using `session_replication_role = replica` can write ineligible grants (demonstrated above). There is no least-privilege database role yet. Restored or imported data must be checked with `organization_membership_eligibility_violation`.
- **The guards run with the writer's privileges** (`SECURITY INVOKER`). A future restricted writer role needs privileges sufficient for their `SELECT … FOR SHARE` on `actor`.
- **No row-level security, no audit-complete identity lifecycle, no grant-change history.**
- **Affiliation provenance.** A stored `provider` value does not prove employment by the provider.
- **Single environment.** One machine, one PostgreSQL 16.14 container, default settings, superuser test role, small synthetic data. No load, crash-recovery, replication, or long-running stress testing.
- **Deadlock victim selection** is PostgreSQL's choice. The natural interleaving aborted the direct writer in every observed run; the provisioning-victim path was exercised only with a raised `deadlock_timeout` on the other session.
- **Developer's existing `eaiso_test`.** These results come from the verification container. A developer database initialized earlier needs migration 003 applied once before these tests can run there.

## Reproduction

With the local container from the [README](../README.md#local-development-postgresql) on port 5432, in **Windows Command Prompt**, from the repository root. Apply migration 003 **once** to each database:

```cmd
docker cp db/003_actor_identity_and_grants.sql eaiso-postgres:/tmp/003_actor_identity_and_grants.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_test -v ON_ERROR_STOP=1 -f /tmp/003_actor_identity_and_grants.sql
```

Run the tests:

```cmd
.\.venv\Scripts\python.exe -m pytest tests/test_identity_grants.py -m "not db" -v
set "TEST_DATABASE_URL=postgresql://eaiso_dev:local-dev-only@127.0.0.1:5432/eaiso_test"
.\.venv\Scripts\python.exe -m pytest tests/test_identity_grants.py -m db -v -s
.\.venv\Scripts\python.exe -m pytest tests -q
set "TEST_DATABASE_URL="
```

`-s` shows the race-outcome counts and the deadlock victim. Do not treat a skipped test as a pass. **Never point `TEST_DATABASE_URL` at `eaiso_dev` or any non-test database.** The development password shown is for an isolated local container only.
