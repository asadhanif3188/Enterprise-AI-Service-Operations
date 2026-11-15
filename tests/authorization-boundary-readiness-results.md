# Authorization Boundary — Security-Readiness Results

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Purpose:** an independent verification gate for the integrated identity, credential-verification, and authorization foundation before any HTTP work. It is not a feature increment.  
**Governing decisions:** [ADR-0004](../docs/adr/0004-keyed-idempotent-case-creation.md), [ADR-0005](../docs/adr/0005-server-verified-identity-and-tenant-authorization.md), and [ADR-0006](../docs/adr/0006-actor-affiliation-and-role-grant-eligibility.md), all Accepted and unchanged by this work  
**Execution date:** 2026-11-15

**Result in one paragraph.** An adversarial review found **no exploitable flaw in the authorization layer** as it exists today. It found **gaps in the structural no-bypass check** that would matter once route code exists, and **four low-severity issues** in the layer and credential resolver. All were fixed within the accepted architecture, each with a regression test that failed before the fix. The full suite then passed on a **fresh PostgreSQL 16 environment** and on the existing verification environment. A restricted, non-superuser database role can run the authorized operations, but **database privileges alone do not isolate customers**. **Recommendation: go** for local HTTP integration, with the conditions in [section 10](#10-http-readiness-recommendation).

**Evidence categories used below:**

| Label | Meaning |
|---|---|
| **VERIFIED** | Observed through executed tests or scripted runs recorded here |
| **REVIEWED** | Supported by source inspection only, not executed |
| **NOT TESTED** | Planned, or not reproducible in this environment |
| **NOT IMPLEMENTED** | The capability does not exist |

## 1. Commit and environment examined

- **Baseline examined:** `8debbfd` (`feat: implement centralized case authorization`), confirmed as `HEAD` and in sync with `origin/main` before work began.
- **Code reviewed:** `src/identity_provisioning.py`, `src/credential_auth.py`, `src/case_authorization.py`, `src/authorized_case_operations.py`, `src/case_service.py`, `scripts/check_case_access_boundary.py`, `db/001`–`003`, the three ADRs, the permission and sequence documents, and the existing tests and evidence.
- **Independent review, in two rounds:**
  - **Round 1:** a separate reviewer with no access to the earlier implementation discussion read the code adversarially, and ran the structural checker on bypass snippets in a scratch tree outside the repository. Its findings were reproduced as tests in this repository before any fix.
  - **Round 2:** a second reviewer examined the fixes themselves. It found remaining gaps and false positives in the first rewrite of the checker, plus two test-hygiene issues. Those were also fixed before commit (F11–F15).

| Environment | Details |
|---|---|
| Client | Windows 11, Python 3.12.6, pytest 9.2.0, psycopg 3.3.9 (binary) |
| **Fresh environment** | New disposable container `postgres:16` (16.14), port 55433, new superuser created at start-up with a password generated for this run and never stored in the repository; databases `eaiso_dev` and `eaiso_test` created empty |
| Existing verification environment | The disposable `postgres:16` container used for the earlier evidence, port 55432, database `eaiso_test` |
| Developer data | The developer's own PostgreSQL container and volume were not touched |

## 2. Security properties reviewed

| Property | Category | Evidence |
|---|---|---|
| Actor identity comes only from a successfully verified opaque credential | **VERIFIED** | Entry points take no actor or scope parameter (signature test); spoofed actor IDs presented as credentials return `Unauthenticated(malformed)`; a `str` subclass can no longer redirect the digest (new regression test) |
| Caller-supplied organization IDs are selectors, not authority | **VERIFIED** | Create writes the organization of the locked membership row; nonexistent and forbidden organizations get the same denial; fresh-environment run checks the persisted owner |
| A requester cannot read or list even its own case | **VERIFIED** | Existing suite and fresh-environment run (`Ahmed reads own case` → `CaseNotFound`) |
| Provider affiliation alone grants nothing | **VERIFIED** | A provider with no grants is denied create, read, and list (existing suite and fresh run) |
| An engineer with access to A cannot reach B without a B grant | **VERIFIED** | Existing suite; fresh run after deactivating the engineer's B grant through provisioning |
| Case ownership comes from persisted data | **VERIFIED** | Read joins the case's stored `organization_id` to the membership; dropping that join is caught by the existing mutation controls |
| Missing and inaccessible cases are indistinguishable | **VERIFIED** for type, reason, and message; **REVIEWED** for timing | Existing suite compares outcomes; timing side channels are a documented ADR-0005 limit |
| Unknown actions and roles deny by default | **VERIFIED** | Policy matrix tests; the role matrix is now read-only at run time (new test) |
| SQL is parameterized and tenant-scoped | **REVIEWED** (every statement), **VERIFIED** (scoping through mutation controls) | All statements use bind parameters; tenant predicates are tested |
| Authorization decisions cannot be supplied by callers | **VERIFIED** for the layer API; **VERIFIED** for route code by the hardened structural check | Routes can obtain the layer only through `operations_from_environment`, which wires the real resolver |
| Database failures fail closed and are not mislabelled as denials | **VERIFIED** | Outage, lock timeout, terminated backend, real deadlock, and unexpected non-driver errors all give `CaseOperationUnavailable` and write nothing |
| An uncertain commit is never reported as success | **VERIFIED** with real connection loss | New proxy tests, in both directions (section 4) |
| Logs and exceptions disclose no credentials or case content | **VERIFIED** | Existing log and chaining tests; new tests assert no chained text-bearing exception |

## 3. Findings

Severities are this gate's classification. The independent reviewer rated the first four structural-check findings **HIGH**. They are recorded here as **MEDIUM** because no route code exists, so nothing is reachable today. Each also requires a route author to write code that review should reject, and ADR-0005 positions the check as a complement to code review. They were fixed regardless, before HTTP work begins.

| # | Severity | Finding | Reachable today? | Resolution |
|---|---|---|---|---|
| F1 | MEDIUM | **Identity forgery through allowlisted modules.** The checker allowlisted whole modules, so a route could import `VerifiedActor` and pass a fake resolver to `AuthorizedCaseOperations`, or import `issue_credential`, `CredentialResolver`, or `FileCredentialStore` to mint credentials for any actor. | No, future routes only | Name-level allowlist for trusted modules; routes construct the layer only through the new `operations_from_environment` factory |
| F2 | MEDIUM | **Private state without `_name` syntax.** `ops.__dict__`, `object.__getattribute__`, `operator.attrgetter`, `inspect.getattr_static`, `__globals__`, `gc.get_objects()`, and `type(ops)(...)` were not flagged. | No | Reflective dunder attributes and reflection helpers are violations; those modules are no longer importable |
| F3 | MEDIUM | **Standard-library deny-list too permissive.** A route could reach the already-loaded driver through `sys.modules`, run processes through `os.system`, or import by string through `pydoc.locate`, `logging.config`, `timeit`, or `typing.get_type_hints`. It could also read the credential store with `open`. | No | Inverted to a small standard-library allowlist plus forbidden names |
| F4 | MEDIUM | **String imports through Pydantic.** `ImportString` and `pydantic.v1.utils.import_string` passed, as did private submodules. | No | Forbidden names and private-module rule |
| F5 | MEDIUM | **Route detection was path-only and could pass vacuously.** Handlers in `src/routes/`, `src/Api/`, `app/`, or the repository root escaped the strict rules or the scan entirely. A missing `src/` or running from another directory reported zero violations. | No | Every non-trusted `src/` module is restricted, the whole repository is scanned (except tests and local environments), a missing `src/` is a violation, and the default root is the repository |
| F6 | LOW | **SQL literal scan gaps.** Bytes literals and derived names such as `organization_membership_eligibility_violation` were not matched. | No | Bytes literals scanned; prefix match on protected names |
| F7 | LOW | **`str` subclass credential.** A subclass passed the format check on its own buffer and overrode `encode()`, so the digest came from another actor's credential. The pre-fix test resolved the *victim's* actor. This requires in-process control, not network input. | In-process only | The resolver accepts only an exact `str` |
| F8 | LOW | **Lone surrogate in title or description.** It escaped as a raw `UnicodeEncodeError` after the row locks were taken, and the exception's `object` attribute carried the submitted text. | In-process callers | Rejected as `InvalidCaseRequest` before any lock, raised without chaining |
| F9 | LOW | **Non-driver errors bypassed outcome mapping.** A non-psycopg error during `COMMIT` escaped raw instead of `CaseCreationOutcomeUnknown`, and during the write it escaped instead of `CaseOperationUnavailable`. | Unlikely, but possible | Any other error maps to `CaseOperationUnavailable("internal")` before commit and to `CaseCreationOutcomeUnknown` at commit, without chaining |
| F10 | LOW (hardening) | **Mutable role matrix.** `ROLE_PERMISSIONS` was a plain dict that in-process code could widen. Verified against the previous version: after mutation, a requester was permitted to read. | In-process only | Read-only `MappingProxyType` |
| F11 | MEDIUM (round 2, in the first rewrite) | **Excluded directory names were skipped under `src/`.** A route package named `src/api/logs/` or `src/api/build/`, or any `src/.hidden/` module, was not scanned at all. | No | Every directory under `src/` is scanned; exclusions apply only outside `src/` |
| F12 | LOW (round 2) | **Alias-then-call.** `m = __import__; m(...)` and `g = getattr` passed, because only direct calls were matched. | No | Any bare use of those built-in names is a violation |
| F13 | LOW (round 2) | **Excluded modules through allowed ones.** `logging.sys.modules[...]` and `uuid.os.system(...)` passed. | No | `sys`, `os`, `modules`, `codecs`, `subprocess`, and `importlib` are forbidden attribute and bare names |
| F14 | LOW (round 2, usability) | **False positives on ordinary route code.** These were flagged in the first rewrite, which would push route authors toward widening the allowlists: `re.compile`, `self._cache`, `HTTPStatus`, `functools`, `time`, REST paths such as `/case_records`, and UTF-8 files with a byte-order mark. | n/a | Attribute calls match only real reflection names; a class's own `self._x` is allowed (another object's `_x`, and `__class__`, remain violations); `functools`, `time`, and `http.HTTPStatus` are allowed; table names match whole identifiers; files are read as `utf-8-sig` |
| F15 | LOW (round 2, tests) | **Test hygiene.** A partially failed restricted-role setup could leave a cluster-wide role behind. The network-cut test's wait for the abandoned session could end early, so its "no case" assertion did not prove a rollback had completed. | n/a | Role creation cleans up on partial failure; the test now waits until the proxied session, identified by its client port, has ended |
| I1 | INFO | `Unauthenticated.reason` distinguishes `missing`, `malformed`, `unknown`, `revoked`, and `actor_not_found`. | n/a | Condition for HTTP: map every reason to one generic `401` body and never echo exception text |
| I2 | INFO | Credential revocation is not serialized with in-flight operations (section 6). | n/a | Observed and documented; clarification proposed |
| I3 | INFO | No supported provisioning operation deactivates an actor. Tests and this run use direct SQL. | n/a | Recommended operator tooling before shared use |

**Checked and safe** (REVIEWED, and VERIFIED where a test exists):

- relative-import tricks such as `from ... import case_service`;
- a direct `import psycopg` in a route;
- the create lock order, which matches provisioning (actor first, then membership), so the two cannot deadlock against each other;
- credential comparison by SHA-256 of a 256-bit secret, so there is no meaningful timing oracle;
- failures raised outside `except` blocks, so no credential or case text is chained.

## 4. New tests and observed results

Every new test states its expected outcome and, where state can change, asserts the persisted database state.

**Regression tests for the findings** (each failed before its fix):

| Test | Before the fix | After |
|---|---|---|
| `test_str_subclass_cannot_redirect_the_digest_to_another_credential` | Resolved to the victim actor | `Unauthenticated(malformed)`; real credential still works |
| `test_invalid_input_rejected_before_database_offline` (2 new surrogate cases) | `CaseOperationUnavailable` (it reached the database stage) | `InvalidCaseRequest` with no connection attempt |
| `test_unexpected_non_driver_error_fails_closed_with_correct_outcome[write]` | Raw `RuntimeError` carrying the description | `CaseOperationUnavailable("internal")`, no chaining, nothing written |
| `test_unexpected_non_driver_error_fails_closed_with_correct_outcome[commit]` | Raw `RuntimeError` | `CaseCreationOutcomeUnknown`, no chaining, nothing written |
| `test_permission_matrix_is_read_only_at_runtime_offline` | Mutation succeeded and a requester could read (checked against the previous module) | `TypeError` |
| `test_readiness_review_bypasses_are_each_flagged` (44 bypass files) | Original checker **missed 41 of 44**; the first rewrite missed **7 of the 9** round-2 cases. (A `src/Api/` case is a separate test because it collides with `src/api/` on case-insensitive file systems) | All 44 flagged |
| `test_permitted_route_module_passes` (now includes ordinary route code) | First rewrite raised **8 false positives** on the ordinary-route module | No violations |
| `test_utf8_bom_file_is_parsed_like_python_does` | Reported as unparseable | Parsed and checked |
| `test_differently_cased_route_directory_is_still_restricted` | Not flagged | Flagged |
| `test_missing_scanned_root_is_a_violation` | Zero violations | `scanned-root-missing` |
| `test_default_root_is_the_repository_not_the_working_directory` | Old default scanned the working directory | Repository root |
| `test_factory_builds_the_layer_with_the_real_resolver_offline` | n/a (new factory) | Real `CredentialResolver` wired |

Against the original checker, **9 of the 14 boundary tests fail**; all 14 pass against the hardened checker. Five existing tests were updated, because the rules tightened:

- the permitted route uses the factory and named imports, and adds ordinary route code;
- `src/` modules outside `src/api/` also report forbidden imports;
- the trusted-module example moved to trusted modules;
- a `scripts` import replaced the now-allowed restricted helper import;
- one dynamic-code count rose from 3 to 4, because the bare name `importlib` is now flagged as well.

**New PostgreSQL evidence** ([`tests/test_authorization_readiness.py`](test_authorization_readiness.py), 6 tests):

| Test | Expected and observed |
|---|---|
| `test_credential_change_during_in_flight_create_does_not_stop_it[revoke]` and `[rotate]` | A create is held at its `INSERT` (after resolution and both `FOR SHARE` locks) while the credential is revoked or rotated. New resolutions are refused at once, but the in-flight create **commits**, with exactly one new case in customer A. The old credential is then refused for create, read, and list with no further writes; a rotated replacement keeps access |
| `test_real_connection_loss_at_commit_is_unknown_outcome[before_server]` | A TCP proxy cuts the connection when it sees `COMMIT` and does not forward it. The layer reports `CaseCreationOutcomeUnknown`; the server rolled back and **no case exists** |
| `test_real_connection_loss_at_commit_is_unknown_outcome[after_server]` | The proxy forwards `COMMIT`, sees the server confirm it, and withholds the reply. The layer reports `CaseCreationOutcomeUnknown`; **the case exists**. Together the two cases show why neither success nor rollback may be claimed |
| `test_real_deadlock_fails_closed_or_create_completes[create]` | Fault injection: another session locks membership, then actor, the reverse of the accepted order. PostgreSQL chose the create as victim (server log: `deadlock detected`, on the create's membership lock). Outcome: `CaseOperationUnavailable("database")`, not a denial, and nothing written |
| `test_real_deadlock_fails_closed_or_create_completes[other_session]` | Same cycle with the other session as victim (server log: `deadlock detected` on its `UPDATE actor`). The create completed normally with one new case |

**Negative control:** with commit failures reported as `CaseOperationUnavailable` instead of unknown, both proxy tests fail (the source was restored byte for byte afterwards).

**Simulated vs real commit failure.** The earlier suite's commit test monkeypatches `commit()` to raise before anything is sent, which simulates only the "COMMIT never arrived" case. The new proxy tests use real sockets in both directions, so the unknown-outcome contract is now exercised against genuine network failures.

## 5. Fresh-environment reproduction result

**VERIFIED.** On the new PostgreSQL 16.14 container:

1. `db/001_init.sql`, `db/002_case_status_events.sql`, and `db/003_actor_identity_and_grants.sql` applied in order with `ON_ERROR_STOP=1` to both `eaiso_dev` and `eaiso_test`; `db/seed_dev.sql` loaded into `eaiso_dev`.
2. Schema-integrity SQL checks on the fresh `eaiso_dev`: **ALL 11 SCHEMA INTEGRITY CHECKS PASSED**.
3. **Independent end-to-end run** (an unversioned scratch driver, so a reader cannot rerun it; every operation it called is committed code, listed in section 11):
   - the synthetic scenario was provisioned with the real CLI (`python -m scripts.provision_dev_identities`, reporting 0 eligibility violations);
   - five fresh credentials were issued with the real credential CLI and held only in memory (the store file contained no credential value);
   - the sequence ran credential → verified actor → policy → scoped operation → persisted result through `operations_from_environment`.

   **35 of 35 checks passed:**

| Operation | Expected | Observed |
|---|---|---|
| Engineer (A and B grants) creates in A, then in B | Created; persisted owner equals the selected customer | As expected for both |
| Manager reads the A case, lists B | Allowed; the B list contains only B cases | As expected |
| Requester A creates in A | Created | Created |
| Requester A reads its own case / lists A / creates in B | `CaseNotFound` / `OrganizationAccessDenied` / `OrganizationAccessDenied` | As expected, state unchanged |
| Requester B reads the A case | `CaseNotFound` | As expected, state unchanged |
| Provider with no grants: create A, read A case, list B | Denied for all three | As expected, state unchanged |
| Actor UUID presented as a credential | `Unauthenticated(malformed)` | As expected, state unchanged |
| Operator deactivates the engineer's B grant; engineer creates in, reads from, or lists B | Denied | As expected, state unchanged; A access retained |
| Operator revokes requester B's credential with the CLI; it is reused | `Unauthenticated(revoked)` | As expected, state unchanged |
| Operator deactivates the manager; the manager creates in A | `InactiveActor` | As expected, state unchanged |

"State unchanged" compares every `case_record`, `organization_membership`, and `actor` row before and after each denied call.

4. **Full test suite on the fresh `eaiso_test`**, with the privilege tests opted in: see [section 8](#8-full-regression-results).

## 6. Credential-revocation timing observations

ADR-0005 distinguishes revocation controls by where they live. Observed behavior:

| Control | Where it lives | Effect on an in-flight create | Effect on later requests | Category |
|---|---|---|---|---|
| **Actor deactivation** | `actor.is_active`, row-locked `FOR SHARE` by create | Serialized: if it commits first, the waiting create denies; if the create holds its locks, the deactivation waits for the create to commit | Denied (`InactiveActor`) | **VERIFIED** (earlier suite) |
| **Membership deactivation** | `organization_membership.is_active`, row-locked `FOR SHARE` | Serialized in the same way | Denied (`OrganizationAccessDenied` / `CaseNotFound`) | **VERIFIED** (earlier suite) |
| **Membership role change** | `organization_membership.role` | Serialized, but every role ADR-0006 allows keeps `case:create`, so it orders and never denies a create | Depends on the role's permissions | **VERIFIED** (earlier suite) |
| **Credential revocation or rotation** | Local file store, outside the database transaction | **Not serialized:** the credential is checked once, before the transaction, and the in-flight create commits | Refused immediately (`Unauthenticated(revoked)`), without restart | **VERIFIED** (new tests) |
| Reads and lists | One snapshot per statement, no row locks | A read that started before any revocation committed completes under its snapshot | Denied after the revocation commits | **VERIFIED** (earlier suite) |

**Is a clarification needed?** ADR-0005 states that "revocation is not instantaneous for requests already in flight" and defines the lock-based ordering for actor and membership rows. It does not say explicitly that credential-store revocation is checked only at resolution and is never re-checked inside the transaction.

- **Behavior:** the observed behavior is consistent with the ADR and creates no exposure beyond an operation already authorized when it began.
- **Proposed clarification, not applied:** a short note in the ADR-0005 implementation evidence or the sequence document that (1) credential revocation affects only later resolutions, and (2) deactivating the actor is the control that serializes with in-flight writes.
- **What was done:** the [sequence document](../docs/authentication-authorization-sequence.md) now describes the observed behavior. The Accepted ADR itself was not changed.
- **Not a blocker** for local HTTP work.

## 7. Database-privilege assessment

All earlier evidence ran as a PostgreSQL **superuser**. A superuser bypasses every grant, can disable triggers, and can set `session_replication_role = replica` to skip the ADR-0006 guards. The application-layer checks therefore protected nothing against that connection itself.

**Investigation** ([`tests/test_database_privilege_boundary.py`](test_database_privilege_boundary.py), 4 tests).

- **Opt-in.** Roles are cluster-wide objects, so the tests run only with `EAISO_ALLOW_TEST_ROLES=1` on a disposable cluster, and are reported as **skipped** otherwise.
- **The role.** Each test creates a uniquely named non-superuser LOGIN role, with a random password generated at run time, and drops it afterwards. Zero test roles remained after the runs.
- **Scope.** This is investigation evidence, **not** a production role or migration.

| Question | Result | Category |
|---|---|---|
| Can `SELECT` on `actor`, `organization_membership`, `case_record` plus `INSERT` on `case_record` run the layer? | **Reads and lists: yes. Create: no.** PostgreSQL requires `UPDATE` privilege on at least one column for `SELECT … FOR SHARE`. The create fails closed as `CaseOperationUnavailable("database")` with nothing written, never as a denial | **VERIFIED** |
| With `UPDATE (created_at)` added on `actor` and `organization_membership`? | **All three operations work** under the restricted role, including the resolver's actor lookup. Application-layer isolation still denies the customer-B case | **VERIFIED** |
| Can that role change its own affiliation, deactivate or create or delete an actor, grant itself customer B, change or deactivate or delete a grant, update or delete a case, create an organization, disable the grant triggers, set `session_replication_role`, or read the eligibility view? | **No.** All 14 attempts failed with `InsufficientPrivilege`; actor, grant, and case rows were unchanged | **VERIFIED** |
| Do database privileges alone isolate customers? | **No.** The same role, holding a grant only in customer A, read customer B's case (title and description) with direct SQL. The column `UPDATE` grant needed for `FOR SHARE` also lets it lock actor rows `FOR UPDATE`, which could block provisioning, and rewrite `created_at` | **VERIFIED** (characterization of the limitation) |

**Conclusions:**

- A non-superuser runtime role is practical. It removes identity and grant tampering and trigger bypass from the application's own connection.
- It **does not** provide tenant isolation. Any connection with `SELECT` on `case_record` can read every customer, so isolation remains **application-layer only**.
- PostgreSQL-enforced isolation would need row-level security or narrowly scoped `SECURITY DEFINER` functions. Either is a **separate architectural decision** and is **NOT IMPLEMENTED**.
- **Recommendation (not applied):** before any shared or production-like deployment, decide in a new ADR on:
  - a dedicated runtime role;
  - how to grant the lock privilege, for example a column chosen for it or a locking function;
  - whether to adopt row-level security.

## 8. Full regression results

Counts are as reported by pytest. "Offline" tests need no database.

| Run | Environment | Executed | Passed | Failed | Skipped | Deselected |
|---|---|---|---|---|---|---|
| Full suite, run 1 | Fresh, privilege opt-in | 277 | 277 | 0 | 0 | 0 |
| Full suite, run 2 | Fresh, opt-in | 277 | 277 | 0 | 0 | 0 |
| Full suite, run 3 | Fresh, opt-in | 277 | 277 | 0 | 0 | 0 |
| Database tests only (`-m db`) | Fresh, opt-in | 123 | 123 | 0 | 0 | 154 |
| Offline tests only (`-m "not db"`) | none | 154 | 154 | 0 | 0 | 123 |
| Full suite | Existing verification environment, **no** opt-in | 273 | 273 | 0 | 4 | 0 |
| Full suite | Existing verification environment, opt-in | 277 | 277 | 0 | 0 | 0 |
| Full suite | No `TEST_DATABASE_URL` | 154 | 154 | 0 | 123 | 0 |
| Schema-integrity SQL | Fresh `eaiso_dev` | 11 checks | 11 | 0 | — | — |
| Structural check on the repository | — | — | 0 violations | — | — | — |

**Per module (fresh run 1):**

| Module | Tests | Change in this gate |
|---|---|---|
| Identity and grant integrity | 87 | none |
| Credential verification | 59 | +1 regression |
| Centralized authorization | 73 | +6: 2 surrogate inputs, 2 unexpected-error cases, read-only matrix, factory |
| Structural boundary | 14 | +5, 5 updated |
| Readiness (new) | 6 | new |
| Database privilege (new, opt-in) | 4 | new |
| Case service | 16 | none |
| AI-triage failure paths | 14 | none |
| Model output | 4 | none |

These runs were made after all fixes, from the final working tree. An earlier batch overlapped a moment when the source files were temporarily reverted for a type-check comparison, so it was discarded and not counted, even though it passed. After the runs, no restricted test roles, actors, or memberships remained in either environment.

The four privilege tests are **skipped, not passed**, wherever the opt-in is absent. mypy reports the same four pre-existing errors in `scripts/run_triage.py` before and after this work, and none in the changed files.

## 9. Remaining risks and limitations

- **No HTTP boundary (NOT IMPLEMENTED).**
  - No route, request parsing, response mapping, or HTTP test vector exists or has been executed.
  - Statuses (`401`/`403`/`404`/`500`) are equivalents carried by typed exceptions.
- **Application-layer isolation only.**
  - No row-level security, and no production database role.
  - The suites ran as a superuser.
  - A privileged SQL client, `src/case_service.py`, or any trusted module can still bypass the layer (**REVIEWED**; section 7 **VERIFIED** the direct-read case).
- **Static analysis limits.** The hardened check does not detect:
  - SQL assembled at run time;
  - code in string annotations or format strings that a framework evaluates;
  - behavior inside allowlisted packages;
  - misuse by trusted modules or reviewers widening the allowlists.

  It reduces accidental and casual bypass; it does not make arbitrary Python safe.
- **Revocation is not instantaneous.** In-flight reads complete under their snapshot, and in-flight creates complete after a credential revocation (section 6).
- **Deadlocks** are possible only if another writer takes membership before actor. Provisioning follows the accepted order, and a deadlock fails closed (**VERIFIED**).
- **Side channels:** timing differences between outcomes are not addressed (ADR-0005 accepted limitation, **NOT TESTED**).
- **Deferred:**
  - keyed idempotent creation (ADR-0004);
  - list cursors;
  - requester status visibility;
  - verified-actor attribution on cases and events;
  - correlation IDs;
  - an actor-deactivation provisioning operation;
  - production identity (OIDC or SSO);
  - credential lifecycle beyond the local test store.
- **Test-environment scope:** one client OS (Windows), PostgreSQL 16 only, local containers.

## 10. HTTP readiness recommendation

**GO for beginning local HTTP integration**, subject to these conditions. None requires changing an Accepted ADR.

1. Route modules live in `src/api/` and use only `operations_from_environment` and the outcome classes. The structural check stays in the test suite, and any new trusted module or allowlist entry is a reviewed change.
2. Map every `Unauthenticated` reason to one generic `401` body. Map `CaseNotFound` to one identical `404` body. Never echo exception text, and keep logs free of credentials and case content.
3. Report `CaseCreationOutcomeUnknown` as an uncertain outcome, never as success. Keyed retry stays disabled until ADR-0004 is implemented.
4. Execute the HTTP test vectors as real HTTP tests before marking them executed.
5. Do not deploy beyond the local machine while the runtime connection is a superuser and row-level security is absent.

**Blockers requiring architectural decisions:** none for local HTTP integration. Before any shared or production deployment:

- a database runtime-role and row-level-security ADR (section 7);
- the production identity provider decision;
- the credential-revocation timing clarification proposed in section 6, which is documentation only.

## 11. Reproduction

```bash
# Structural check (default root is the repository)
python -m scripts.check_case_access_boundary          # 0 violations

# Fresh disposable PostgreSQL 16 (choose your own throwaway password)
docker run -d --name eaiso-fresh -e POSTGRES_USER=eaiso_fresh -e POSTGRES_PASSWORD=<throwaway> \
  -e POSTGRES_DB=eaiso_dev -p 127.0.0.1:55433:5432 postgres:16
docker exec eaiso-fresh createdb -U eaiso_fresh eaiso_test
for db in eaiso_dev eaiso_test; do
  for f in 001_init.sql 002_case_status_events.sql 003_actor_identity_and_grants.sql; do
    docker exec -i eaiso-fresh psql -q -v ON_ERROR_STOP=1 -U eaiso_fresh -d $db < db/$f
  done
done
docker exec -i eaiso-fresh psql -q -v ON_ERROR_STOP=1 -U eaiso_fresh -d eaiso_dev < db/seed_dev.sql
docker exec -i eaiso-fresh psql -v ON_ERROR_STOP=1 -U eaiso_fresh -d eaiso_dev < tests/test_schema_integrity.sql

# Full suite, including the opt-in privilege tests (disposable cluster only)
export TEST_DATABASE_URL="postgresql://eaiso_fresh:<throwaway>@127.0.0.1:55433/eaiso_test"
export EAISO_ALLOW_TEST_ROLES=1
python -m pytest tests -q -rs

# Only the new evidence
python -m pytest tests/test_authorization_readiness.py tests/test_database_privilege_boundary.py -v

# End-to-end operator path on eaiso_dev
python -m scripts.provision_dev_identities --database-url postgresql://eaiso_fresh:<throwaway>@127.0.0.1:55433/eaiso_dev
python -m scripts.manage_test_credentials --credentials-file .local/fresh-creds.json \
  issue --actor-id <actor uuid> --database-url postgresql://eaiso_fresh:<throwaway>@127.0.0.1:55433/eaiso_dev
# then call operations_from_environment(<eaiso_dev url>, Path(".local/fresh-creds.json"))
# with the printed credential; .local/ is git-ignored.

docker rm -f eaiso-fresh
```

Without `EAISO_ALLOW_TEST_ROLES=1`, the four privilege tests are skipped, so never set it on a cluster you care about. Without `TEST_DATABASE_URL`, every PostgreSQL-backed test is skipped.
