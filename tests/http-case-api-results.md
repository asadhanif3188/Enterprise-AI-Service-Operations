# Local HTTP Case API — Validation Results

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Scope:** the localhost-only HTTP API in [`src/api/`](../src/api/) (`POST /cases`, `GET /cases/{id}`, `GET /cases`), its runner [`scripts/run_local_api.py`](../scripts/run_local_api.py), and the HTTP tests in [`tests/test_http_case_api.py`](test_http_case_api.py)  
**Governing decisions:** [ADR-0003](../docs/adr/0003-application-controlled-case-transitions.md), [ADR-0004](../docs/adr/0004-keyed-idempotent-case-creation.md), [ADR-0005](../docs/adr/0005-server-verified-identity-and-tenant-authorization.md), [ADR-0006](../docs/adr/0006-actor-affiliation-and-role-grant-eligibility.md); all Accepted and unchanged  
**Execution date:** 2026-11-15

> **Later change.** The interim `501 idempotency_not_implemented` behavior recorded below was replaced on 2026-11-16 by keyed idempotent creation (ADR-0004): a valid `Idempotency-Key` now gives `201`, `200` replay, `422`, or `503`, and a malformed one gets `400`. See the [keyed case creation results](keyed-case-creation-results.md). The rest of this report describes the API as originally recorded.

**Result.** All three endpoints work through the centralized authorized layer. **95 HTTP tests passed** (72 offline, 23 PostgreSQL-backed), with 0 failed and 0 skipped, on a fresh PostgreSQL 16 environment. HTTP-level tenant isolation is demonstrated against PostgreSQL, with database-state assertions. A real server process was also exercised on `127.0.0.1:8000` (14 of 14 checks passed). The full repository suite passed: **372 tests** in three consecutive runs.

**What this is not.** It is a controlled local demonstration, not a shared or production service:
- there is no keyed idempotent creation (interim `501`) and no cursor pagination;
- there are no status transitions over HTTP and no production authentication;
- tenant isolation is not enforced by the database.

The development database connection may be a superuser, and PostgreSQL row-level security is absent.

**HTTP tests vs Python-layer tests.** Everything below labelled *HTTP* was executed through FastAPI's `TestClient`, or through a real server process. Each request ran through the real routing, request parsing, and exception handlers, then the trusted factory, the credential resolver, the authorized layer, and PostgreSQL. The earlier Python-layer results ([case authorization](case-authorization-results.md), [readiness](authorization-boundary-readiness-results.md)) call the layer directly. They are not HTTP evidence and are not counted here.

## 1. Commit and environment

| Item | Value |
|---|---|
| Baseline | `c0ef534` (`test: validate integrated authorization security boundary`), confirmed as `HEAD` with a clean working tree |
| Client | Windows 11, Python 3.12.6, pytest 9.2.0, psycopg 3.3.9 |
| HTTP framework | FastAPI 0.163.1, Starlette 1.10.0, uvicorn 0.54.0; test client transport httpx2 2.16.0 (Starlette 1.x deprecates plain `httpx` for `TestClient`) |
| Dependency file | [`requirements-api.txt`](../requirements-api.txt): `fastapi>=0.115,<1`, `uvicorn>=0.30,<1`, `httpx2>=2.16,<3`, plus the existing [`requirements-ai.txt`](../requirements-ai.txt) |
| Fresh environment | New disposable `postgres:16` (16.14) container on `127.0.0.1:55433`, superuser with a password generated for the run (never stored in the repository), empty `eaiso_dev` and `eaiso_test`, migrations 001–003 applied in order, seed data in `eaiso_dev` |
| Existing environment | The earlier disposable verification container (`127.0.0.1:55432`, `eaiso_test`) |

## 2. Server configuration and loopback binding

- **Runner.** [`scripts/run_local_api.py`](../scripts/run_local_api.py) calls `uvicorn.run("src.api.app:app", host="127.0.0.1", ...)`. There is no host option, and ports must be unprivileged. Running `uvicorn --host 0.0.0.0` directly would bypass this and is documented as not allowed.
  - **VERIFIED (offline test):** the runner passes `127.0.0.1`, and `--host` is rejected.
  - **VERIFIED (real process):** `netstat` showed the only listening socket on port 8000 was `127.0.0.1:8000`.
- **Configuration.** At startup, the application lifespan calls `operations_from_environment()` with no arguments. The database URL comes from the server's `DATABASE_URL`, and the credential store from `EAISO_CREDENTIALS_FILE` (default `.local/test-credentials.json`).
  - **VERIFIED:** startup fails, with fixed text, when `DATABASE_URL` is missing, blank, unparseable, or has no database name, and when the credential store is missing. Planted secret text in an invalid URL appeared neither in the error nor in its exception chain.
- **Factory change.** `operations_from_environment(dsn=None, store_path=None)` now reads and validates `DATABASE_URL` when no DSN is passed, raising the new `OperationsConfigurationError`. This was the only change to a trusted module. Routes still cannot import the factory's internals, the layer class, the resolver, or the store.
- **Routes.** Exactly three. The interactive documentation and OpenAPI routes are disabled, and slash redirects are off.
  - **VERIFIED:** `/docs`, `/redoc`, `/openapi.json`, `/admin`, `/credentials`, `/actors`, `/memberships`, and `PATCH`/`PUT`/`DELETE`/`POST` on `/cases/{id}` (including `/transitions` and `/status`) all return `404` or `405` in the error envelope.

## 3. Routes implemented

| Route | Layer call | Success |
|---|---|---|
| `POST /cases` | `create_case(credential, organization_id, title, description)` | `201`, `Location: /cases/{id}`, persisted representation at `NEW`, version `1` |
| `GET /cases/{id}` | `get_case(credential, case_id)`; ownership from the stored case | `200`, full representation |
| `GET /cases?organization_id=…&limit=…` | `list_cases(credential, organization_id, limit=…)` | `200`, `{"items": [...], "has_more": bool}`, ordered by `(created_at, id)`, no descriptions; `limit` default 20, maximum 100 |

Every route passes the **raw bearer token** to the layer. The routes contain no SQL, no policy, and no identity logic, and never call `src/case_service.py`.

## 4. Authentication handling

- **Header syntax.** Exactly one `Authorization` header whose value is `Bearer <token>`, with the scheme case-insensitive, one space, and an RFC 6750 b64token. Anything else gets the generic `401`. This covers a missing header, duplicates (even of two *valid* credentials), comma-joined values, other schemes, and stray whitespace. The token itself is verified only by the resolver, inside the layer, exactly once per request.
- **No other identity source.** `X-Actor-Id`, `X-User-Id`, `X-Org-Id`, body fields, display names, and claimed memberships are never read. A body `actor_id` is rejected as an unknown field.
- **One `401`.** Every `Unauthenticated` reason produces a byte-identical body and identical headers (`WWW-Authenticate: Bearer`): missing, malformed header, malformed token, unknown, revoked, and actor not found.
- **Processing order** (documented in [`src/api/cases.py`](../src/api/cases.py) and [sequence §7.2](../docs/authentication-authorization-sequence.md#72-implemented-local-http-api), and tested):
  1. `Idempotency-Key` rejection (`POST`).
  2. `Authorization` header syntax.
  3. Request syntax: parameters, content type, JSON, and body schema.
  4. Credential verification in the layer.
  5. Value checks (canonical UUIDs, text, `limit` range).
  6. Authorization and the operation.

  **Request-syntax errors therefore precede credential verification.** Malformed JSON with an unknown credential returned `400`, and a schema-invalid body returned `422`. **Value errors follow it.** A blank title, a malformed path or query UUID, or `limit=0` with an unknown credential returned `401`; with a valid credential they returned `422` or `400`. The framework does not preserve the Python layer's authentication-first order for syntax errors, and this report does not claim it does.

## 5. Error mappings

Envelope: `{"error": {"code": "...", "message": "..."}}`, with fixed server text. Server errors add `request_id`, and every response carries a server-generated `X-Request-ID`; a client-supplied `X-Request-ID` is ignored.

| Domain outcome or condition | HTTP | `code` | Verified by |
|---|---|---|---|
| Every `Unauthenticated` reason; bad `Authorization` header | `401` + `WWW-Authenticate: Bearer` | `unauthenticated` | S01 test, offline header tests, duplicate-header test |
| `InactiveActor` | `403` | `actor_inactive` | V14 |
| `OrganizationAccessDenied`, including a nonexistent organization | `403` | `organization_access_denied` | V07, V10, V11, S02 |
| `CaseNotFound`: missing, foreign, unreadable, or requester's own | `404`, byte-identical | `case_not_found` | V06, V08, V09 |
| Missing, unknown, or duplicate query parameter; non-integer `limit`; malformed JSON (including duplicate keys and excessive nesting) | `400` | `missing_parameter`, `unknown_parameter`, `duplicate_parameter`, `invalid_parameter`, `malformed_json` | offline tests |
| Path or query value invalid after authentication (UUID form, `limit` range) | `400` | `invalid_parameter` | ordering test |
| `cursor` parameter | `400` | `cursor_not_supported` | offline tests, S06 |
| Body missing, extra, or wrongly typed fields; body values invalid after authentication | `422` | `invalid_request` (`details` name only known fields and the problem type) | offline tests, S03, ordering test |
| Non-JSON `Content-Type` | `415` | `unsupported_media_type` | offline tests |
| Body over 64 KiB | `413` | `payload_too_large` | offline test |
| Any `Idempotency-Key` on `POST /cases` | `501` | `idempotency_not_implemented` | offline and database tests |
| `CaseOperationUnavailable`, `AuthenticationUnavailable`, unexpected errors | `500` + `request_id` | `internal_error` | S05, credential-store failure test |
| `CaseCreationOutcomeUnknown` | `500` + `request_id`, never `201`, no `Location` | `case_creation_outcome_unknown` ("may or may not have been created … retrying may create a duplicate") | real connection-loss tests |
| Unknown route / method | `404` / `405` | `route_not_found` / `method_not_allowed` | offline tests |

**Interim idempotency.** Any `Idempotency-Key` header gets `501`, whatever its value or the case of its name, and including an underscore variant. This happens before authentication, so no create is attempted. The value is never parsed, stored, logged, or echoed. This **differs from the future contract**: once ADR-0004 is implemented, a syntax failure gets `400` and a replay `200` ([API contract §3.1](../docs/api-contract.md#31-implemented-interim-behavior)). It does **not** fulfil the keyed-create contract, and no keyed-creation test is claimed. Two identical unkeyed creates produced two distinct cases.

## 6. HTTP security tests executed

Test-vector coverage. V and S identifiers are from the [permission policy](../docs/authorization-permissions.md#6-proposed-test-vectors) and the [sequence](../docs/authentication-authorization-sequence.md#10-future-negative-test-cases).

| Vectors | HTTP test | Result |
|---|---|---|
| V01, V02, V03 | `test_v01_v03_engineer_creates_reads_and_lists_in_a` | Passed |
| V04 | `test_v04_manager_creates_and_lists_in_its_customer` | Passed |
| V05, V06, V07 | `test_v05_v07_requester_creates_but_cannot_read_or_list` | Passed |
| V08, V09, V10, V11 | `test_v08_v11_engineer_of_a_cannot_create_read_or_list_b` | Passed |
| V13 | `test_v13_inactive_membership_denies` | Passed |
| V14 | `test_v14_inactive_actor_is_403_without_insert` | Passed |
| V15, V17 | `test_v15_v17_provider_without_grants_has_no_case_access` | Passed |
| V18, S01 | `test_s01_every_unauthenticated_reason_gets_the_identical_401` (and offline header tests) | Passed |
| V19, V20, S03 | `test_v19_v20_s03_spoofed_identity_headers_and_body_fields_never_override_the_credential` | Passed |
| V21 | offline `missing_parameter` tests; ordering test | Passed |
| V22 | `test_no_transition_administrative_or_documentation_routes_offline` | Passed |
| S02 | `test_s02_nonexistent_organization_is_identical_to_forbidden` | Passed |
| S04 | structural check over `src/api/` (section 9) | Passed |
| S05 | `test_s05_database_outage_is_generic_500_without_writes` | Passed |
| S06 | cursor rejection (no cursor is issued) | Passed (interim) |
| S07 | `test_s07_revocation_committing_first_makes_waiting_http_create_deny` | Passed (revocation-first ordering, confirmed with `pg_blocking_pids()`) |
| S10 | `test_no_response_or_log_leaks_secrets_case_text_or_sql` | Passed |
| V12 | — | Infeasible under ADR-0006: no actor may hold both an engineer grant and a requester grant |
| V16 | — | Policy-level only (offline policy-matrix tests), not an HTTP test |
| S08, S09 | — | Not implemented (keyed creation) |

**Additional HTTP tests:**
- multiple grants stay independent, and membership revocation applies to the next request;
- credential rotation (old credential `401`, replacement keeps the actor);
- bounded and ordered listing with `has_more`;
- duplicate `Authorization` headers carrying valid credentials;
- the validation and authentication ordering;
- `Idempotency-Key` never creates a case;
- a credential-store failure fails closed;
- an unknown commit outcome is never `201`;
- startup configuration failures;
- loopback binding.

**Negative controls.** Each mutation was applied alone, the HTTP tests were run, and the source was restored byte for byte. Every mutation was caught:

| Mutation | HTTP tests failed |
|---|---|
| `Idempotency-Key` ignored | 6 |
| First of duplicate `Authorization` headers used | 1 (after adding a test with two valid credentials; the first version of the suite missed this) |
| `cursor` silently ignored | 2 |
| Extra body fields ignored | 6 |
| `CaseNotFound` mapped to `403` | 6 |
| `Unauthenticated` reason exposed in the body | 15 |
| Unknown commit outcome reported as `201` | 2 |
| Layer read drops the ownership join | 4 |
| Layer list drops the tenant filter | 4 |

## 7. Tenant-isolation results (HTTP, PostgreSQL)

- **Engineer with only customer A:**
  - create in B → `403` with **no insert** (state snapshot unchanged);
  - read a B case → `404`, byte-identical to a nonexistent case (same body and headers apart from `X-Request-ID`);
  - list B → `403`.
- **Requester in A:**
  - create in A → `201`;
  - read its own new case → the same `404` as another A case;
  - list A → `403`.
- **Provider without grants:** list → `403`, read → `404`, create → `403`, no insert.
- **Engineer with A and B grants:**
  - each list returns only that customer's case;
  - after the B grant was deactivated, B read → `404`, B list → `403`, B create → `403`, while A access was retained.
- **Spoofed `X-Actor-Id`, `X-User-Id`, or `X-Org-Id` naming customer B or another engineer:** no effect.
- **Mismatched body selector:** a create whose body selected A but whose headers named B was stored in A.
- **Real server, fresh environment** (`run_local_api` process):
  - requester B reading an A case → `404`;
  - requester B listing A → `403`;
  - requester A creating in B → `403`.

## 8. Database-state assertions, failure and revocation observations

- **Successful creates:** the response equals the persisted row field for field, including timestamps; status is `NEW`, version is `1`, and `Location` matches the new ID.
- **Denied operations:** every denied create (`401`, `403`, `415`, `422`, `501`, outage) compares all `case_record` rows in the test customers, plus the total case count, before and after. All were unchanged.
- **Database outage:**
  - **Case database down** (the layer's connection target replaced with an unreachable address, identity lookups still working): create, read, and list → generic `500`, no write.
  - **Resolver's database down:** the same generic `500`.
  - Neither returned `403` or `404`.
- **Credential store damaged after startup:** `500`, no write. After the file was repaired, the next create succeeded without a restart.
- **Unknown commit outcome, real connection loss** (a TCP proxy cut the connection at `COMMIT`). Both cases returned `500` `case_creation_outcome_unknown` with no `Location`, never `201`:
  - when the `COMMIT` was never forwarded, no case exists;
  - when the `COMMIT` reached the server and its reply was withheld, the case exists.
- **Revocation:**
  - membership deactivation and credential revocation or rotation affect the **next** HTTP request;
  - a membership revocation that commits first makes a waiting HTTP create return `403` with no insert (S07).
  - In-flight behavior under the accepted lock contract, and credential revocation during an in-flight create, are recorded at the Python layer ([readiness §6](authorization-boundary-readiness-results.md#6-credential-revocation-timing-observations)).
- **Leaks:** across a battery of `401`, `403`, `404`, `422`, and `500` responses, the following never appeared in any response body or header, nor in any captured log record (at DEBUG, including the request log):
  - credentials, case title and description, SQL keywords, table names, `psycopg`, tracebacks, and the database URL;
  - `Idempotency-Key` values.

  The real server's output contained no credential value. The request log records method, path, status, and request ID only.

## 9. Structural-check results

`python -m scripts.check_case_access_boundary` → **0 violations**, with the new HTTP modules (`src/api/app.py`, `cases.py`, `errors.py`, `schemas.py`, `__init__.py`) in place. **No change was made to the checker, and no module or name was added to any allowlist.**

The routes import only:
- the layer factory `operations_from_environment` and `DEFAULT_LIST_LIMIT`;
- outcome classes from `src.case_authorization` and `src.credential_auth`;
- FastAPI, Starlette, and Pydantic;
- allowlisted standard-library modules (`json`, `re`, `logging`, `uuid`, `contextlib`, `collections.abc`).

They do not import `os`, `psycopg`, provisioning, credential management, `AuthorizedCaseOperations`, `VerifiedActor`, or `src.case_service`. They use no reflection or private attributes and contain no protected-table SQL. `scripts/run_local_api.py` imports `uvicorn`, which is a server rather than a listed web framework, and it holds no route code.

**Independent review.** A separate adversarial review probed:
- header parsing, query and body tricks;
- error leaks, request IDs, and startup failures;
- the bind address.

It found no high or medium issues, and three low ones, each fixed with a regression test that failed first:
- deeply nested JSON raised an unhandled `RecursionError` (anonymous `500`); it now gets `400`;
- trailing-slash redirects built `Location` from the client's `Host` header; they are now disabled;
- an underscore variant of `Idempotency-Key` was not rejected; it now is.

## 10. Full regression results

| Run | Environment | Executed | Passed | Failed | Skipped | Deselected |
|---|---|---|---|---|---|---|
| Full suite, run 1 | Fresh, privilege opt-in | 372 | 372 | 0 | 0 | 0 |
| Full suite, run 2 | Fresh, opt-in | 372 | 372 | 0 | 0 | 0 |
| Full suite, run 3 | Fresh, opt-in | 372 | 372 | 0 | 0 | 0 |
| Database tests only (`-m db`) | Fresh, opt-in | 146 | 146 | 0 | 0 | 226 |
| Offline tests only (`-m "not db"`) | none | 226 | 226 | 0 | 0 | 146 |
| HTTP tests only | Fresh | 95 | 95 | 0 | 0 | 0 |
| HTTP database tests only | Fresh | 23 | 23 | 0 | 0 | 72 |
| Full suite | Existing environment, **no** privilege opt-in | 368 | 368 | 0 | 4 | 0 |
| Full suite | No `TEST_DATABASE_URL` | 226 | 226 | 0 | 146 | 0 |
| Schema-integrity SQL | Fresh `eaiso_dev` | 11 checks | 11 | 0 | — | — |
| Real server process | Fresh `eaiso_dev`, `127.0.0.1:8000` | 14 checks | 14 | 0 | — | — |

After these runs, route dependency declarations were rewritten from default-argument `Depends(...)` to the equivalent `Annotated[..., Depends(...)]` form (a lint-only change with the same parameter order). A fourth full run on the fresh environment then passed **372 of 372**, and the HTTP and structural-boundary modules passed 109 of 109.

**Per module (fresh):** identity and grant integrity 87; credential verification 59; centralized authorization 73; structural boundary 14; readiness 6; database privilege 4 (opt-in); **HTTP case API 95 (new)**; case service 16; AI-triage failure paths 14; model output 4.

Skipped tests are never counted as passed. The four database-privilege tests run only with `EAISO_ALLOW_TEST_ROLES=1`, and only on a disposable cluster. Afterwards, no test roles, actors, or cases remained in the fresh `eaiso_test`.

## 11. Known limitations

- **Not implemented (deferred):**
  - ADR-0004 keyed idempotency; any key gets `501`, and a blind retry after an unknown outcome can create a duplicate;
  - cursor pagination (`has_more` only);
  - status transitions, human approval, and AI-suggestion actions over HTTP;
  - verified-actor attribution on cases and events;
  - production authentication (OIDC or SSO);
  - database-enforced tenant isolation (row-level security or a least-privilege runtime role).
- **Local only.** The API serves plain HTTP on loopback, with no TLS, rate limiting, or lockout. The development connection may be a superuser.
- **Ordering.** Request-syntax errors (`400`, `413`, `415`, `422`) are reported before the credential is verified. They reveal nothing about tenants, cases, or credentials, but unauthenticated callers can see them.
- **Logging.** The request ID appears in HTTP log lines and server-error bodies; the layer's own log lines do not yet carry it. The request log records paths, which include case IDs.
- **Not measured:** query plans and performance; timing side channels between outcomes.
- **Coverage:** one client OS (Windows), PostgreSQL 16 only, a single-process local server.

## 12. Reproduction

```bash
pip install -r requirements-api.txt
python -m scripts.check_case_access_boundary                  # 0 violations

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

# HTTP tests (offline part needs no database)
python -m pytest tests/test_http_case_api.py -m "not db" -v
export TEST_DATABASE_URL="postgresql://eaiso_fresh:<throwaway>@127.0.0.1:55433/eaiso_test"
python -m pytest tests/test_http_case_api.py -v
EAISO_ALLOW_TEST_ROLES=1 python -m pytest tests -q -rs          # full suite, disposable cluster only

# Real server on loopback
export DATABASE_URL="postgresql://eaiso_fresh:<throwaway>@127.0.0.1:55433/eaiso_dev"
python -m scripts.provision_dev_identities --database-url "$DATABASE_URL"
python -m scripts.manage_test_credentials issue --actor-id <actor uuid> --database-url "$DATABASE_URL"
python -m scripts.run_local_api                                  # http://127.0.0.1:8000
curl -i -H "Authorization: Bearer <credential>" "http://127.0.0.1:8000/cases?organization_id=<org uuid>"

docker rm -f eaiso-fresh
```
