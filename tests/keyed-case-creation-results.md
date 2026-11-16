# Keyed Idempotent Case Creation — Validation Results

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Scope:** keyed case creation as accepted in [ADR-0004](../docs/adr/0004-keyed-idempotent-case-creation.md). It covers:
- migration [`db/004_case_creation_idempotency.sql`](../db/004_case_creation_idempotency.sql);
- the RFC 8785 fingerprint in [`src/request_fingerprint.py`](../src/request_fingerprint.py);
- the RFC 9651 Item parser in [`src/api/structured_fields.py`](../src/api/structured_fields.py);
- `create_case_keyed` in [`src/authorized_case_operations.py`](../src/authorized_case_operations.py);
- `POST /cases` with `Idempotency-Key` in [`src/api/cases.py`](../src/api/cases.py).

**Governing decisions:** ADR-0004 (Accepted 2026-11-15, unchanged), [ADR-0005](../docs/adr/0005-server-verified-identity-and-tenant-authorization.md), and [ADR-0006](../docs/adr/0006-actor-affiliation-and-role-grant-eligibility.md)  
**Execution date:** 2026-11-16

**Result.** Keyed creation works through the authenticated, authorized HTTP API against PostgreSQL:
- `201` for a new case, an identity-only `200` replay, `422` `idempotency_payload_mismatch`, and `503` after the bounded wait;
- `400` for malformed keys, which never fall back to unkeyed creation.

**143 new tests** (113 offline, 30 PostgreSQL-backed) and the full suite (**528 tests**, three consecutive runs) passed on a fresh PostgreSQL 16 environment. All ten deliberate code breakages were caught. This does **not** claim exactly-once network delivery, untested race freedom, or production readiness: records never expire, and isolation is application-layer only.

**Evidence types.** *Real fault* means a terminated PostgreSQL backend, a TCP connection cut at `COMMIT`, or real concurrent sessions confirmed with `pg_blocking_pids()`. *Simulated* means a patched driver call. Both are labelled below.

## 1. Environment and baseline

| Item | Value |
|---|---|
| Baseline | `2a93b95` (`feat: expose authorized case operations through local HTTP API`), confirmed as `HEAD` with a clean working tree |
| Client | Windows 11, Python 3.12.6, pytest 9.2.0, psycopg 3.3.9, FastAPI 0.163.1, Starlette 1.10.0, httpx2 2.16.0 |
| New dependencies | **None.** The RFC 9651 parser and the RFC 8785 serializer are minimal in-repository implementations, verified against the RFC examples |
| Fresh environment | New disposable `postgres:16` (16.14) on `127.0.0.1:55433`; superuser password generated for the run and not stored; empty `eaiso_dev` and `eaiso_test` |
| Existing environment | The earlier disposable verification container (`127.0.0.1:55432`), with migration 004 applied |

## 2. Migration outcome

- **Fresh environment:** migrations 001 → 002 → 003 → 004 applied in order, with `ON_ERROR_STOP`, to `eaiso_dev` and `eaiso_test`. Schema-integrity SQL: **11 of 11 checks passed**.
- **Re-run:** running 004 a second time fails on its first statement inside its transaction, so nothing partial is applied.
- **Starting state:** the table starts empty, with no fabricated historical records.
- **Migrations 001–003:** unchanged. 004 adds one unique constraint to `case_record` and creates `case_creation_idempotency`.

| Constraint | Mechanism | Verified by |
|---|---|---|
| Scope uniqueness `(organization_id, actor_id, operation, idempotency_key)` | Primary key; also the concurrency arbiter | duplicate-scope insert → `UniqueViolation`; concurrency tests |
| Organization and actor exist | Foreign keys, `ON DELETE RESTRICT` | unknown actor → `ForeignKeyViolation` |
| Case exists **and belongs to the record's organization** | Composite foreign key `(organization_id, case_id)` → `case_record (organization_id, id)`, backed by the new `UNIQUE (organization_id, id)`, `DEFERRABLE INITIALLY DEFERRED` | another customer's case → `ForeignKeyViolation`; nonexistent case → `ForeignKeyViolation` (at commit) |
| Completed record has a case | `case_id NOT NULL`; one record per case (`UNIQUE (case_id)`) | `NULL` → `NotNullViolation`; second record for a case → `UniqueViolation` |
| Operation vocabulary | `CHECK (operation IN ('case:create'))` | `case:read` → `CheckViolation` |
| Key shape | `CHECK (idempotency_key ~ '^[A-Za-z0-9._~-]{16,128}$')` (case-sensitive) | too short, too long, or containing a space → `CheckViolation` |
| Fingerprint version | `CHECK (fingerprint_version = 1)` | version 2 → `CheckViolation` |
| Digest form | `CHECK (request_fingerprint ~ '^[0-9a-f]{64}$')` | uppercase hex, or 63 characters → `CheckViolation` |
| Keyed cases cannot be deleted | `ON DELETE RESTRICT` | deleting a keyed case → `ForeignKeyViolation` |
| Ownership checked at commit, enabling reserve-first | `DEFERRABLE INITIALLY DEFERRED` | a record inserted before its case in the same transaction commits; a record whose case never appears is accepted at `INSERT` and rejected at `COMMIT` |

No title or description uniqueness was introduced.

## 3. Header parser conformance (RFC 9651)

[`src/api/structured_fields.py`](../src/api/structured_fields.py) implements the Item algorithms of RFC 9651 §4.2 (bare items, parameters, keys, integers and decimals, strings, tokens, byte sequences, booleans, dates, display strings). Lists and dictionaries are not parsed.

- **Byte sequences** are validated for base64 syntax without being decoded, because only the item's type matters here.
- **Standard examples:** every RFC 9651 §3 example parses to its documented value: `42`, `-42`, `4.5`, `"hello world"`, `foo123/456`, the byte sequence, `?1`, `@1659578233`, the display string, and `5; foo=bar`.
- **Strings:** cases in the style of the HTTP WG test suite — empty, long, whitespace-only, `\"` and `\\` escapes, and an escape before the closing quote — are accepted.
- **Rejected:** non-ASCII text, tab, newline, single quotes, an unbalanced string, bad escapes, two items, a list, an empty value, a leading tab, whitespace before parameters, an uppercase parameter key, and a dangling `;`.
- **Numbers and other types:**
  - limits are enforced: integers up to 15 digits, decimals up to 12 integer digits and 3 fraction digits, and no trailing dot;
  - booleans other than `?0` or `?1` are rejected;
  - a date must be an integer;
  - base64 must be well formed;
  - display strings require lowercase percent-hex and valid UTF-8.
- **Parameters** are parsed (later duplicates win) and ignored for the key: `"<key>"; ttl=60` gives the bare key.

**Over HTTP:**
- **Rejected with `400` `invalid_idempotency_key`:**
  - an unquoted token, an unquoted UUID, an integer, or a byte sequence or boolean;
  - a String that is too short or too long, contains a space or a backslash, or has an invalid escape;
  - an unterminated String, an empty value, a two-item list, or non-ASCII bytes;
  - two field instances (equal or different), and an underscore spelling (`Idempotency_Key`), alone or alongside the real header.
- **Accepted:** the name in any letter case (HTTP field names are case-insensitive), and surrounding spaces.
- **Ordering:** key syntax is checked before authentication, so an invalid key with no credential gets `400`.
- **No mutation:** malformed keys sent with a valid credential changed no case or record. That covered tokens, integers, booleans, strings over 128 characters, non-ASCII bytes, empty values, and lists.
- **Case-sensitive keys:** two keys that differ only in letter case created two cases and two records.
- **Duplicate JSON properties** in a keyed request: `400` `malformed_json`, no case and no reservation; a valid request with the same key then created the case.

## 4. Fingerprint conformance (RFC 8785, version 1)

| Check | Result |
|---|---|
| RFC 8785 §3.2.2 main example (numbers, escapes, literals) | Byte-identical to the RFC's canonical output |
| RFC 8785 §3.2.3 sorting example (UTF-16 code-unit order, including a surrogate pair) | Identical |
| Number serialization (Appendix B samples and ECMAScript boundaries: `0`, `-0`, `5e-324`, `1.7976931348623157e+308`, `9007199254740992`, `1e+21`, `100000000000000000000`, `0.000001`, `1e-7`, …) | All as specified |
| Rejected: NaN, Infinity, integers beyond 2^53, lone surrogates (in values and keys), non-string keys, non-JSON types | All rejected |
| ECMAScript string escaping (`\b \t \n \f \r \" \\`, `\u00xx` for other controls, literal U+007F and U+2028, unescaped `/`) | As specified |
| Golden vector: SHA-256 of a hand-written canonical string | Equal |
| Escaped `é` vs literal `é`; uppercase vs lowercase organization UUID | Same fingerprint (offline, and as a `200` replay over HTTP) |
| NFC vs NFD, trailing space, and letter case | Different fingerprints (offline, and `422` over HTTP) |
| Reordered properties and extra JSON whitespace | `200` replay over HTTP |
| Duplicate JSON properties, unknown properties, lone surrogates | Rejected before fingerprinting (`400`, `422`, `422`) |

Only the digest and `fingerprint_version = 1` are stored. Neither the payload nor the canonical form is stored.

## 5. Transaction arbitration algorithm

Implemented in `create_case_keyed`, as one short READ COMMITTED transaction:

1. **Authorize (ADR-0005).** Lock the actor row `FOR SHARE`, then the membership row `FOR SHARE` in a separate statement, and require `case:create`. A denial rolls back before any key is touched.
2. **Reserve.** `INSERT INTO case_creation_idempotency (…, case_id) VALUES (…, <new UUID>) ON CONFLICT (scope) DO NOTHING RETURNING case_id`. The authorized organization comes from the locked membership row and the actor from the credential resolver. `lock_timeout` is set to the bounded wait for this statement only and restored afterwards.
3. **Won.** Insert the case with that UUID, then `COMMIT`. The deferred composite foreign key is checked at commit, so the case and its record commit together or not at all.
4. **Lost** (a committed record exists). `SELECT` it in the same scope, roll back, and return a replay (equal version and digest) or `422`.
5. **Waiting.** If an uncommitted record holds the scope, the reservation statement waits for that transaction:
   - if it commits, step 4 follows;
   - if it rolls back, the reservation proceeds;
   - if the wait expires, `LockNotAvailable` is returned as `503`, and this request wrote nothing.

**Why it cannot duplicate cases.**
- **One winner.** Only a request whose reservation insert succeeded inserts a case. The primary key admits one committed reservation per scope, so at most one case per scope can commit.
- **No case left by a loser.** A losing request never inserts a case, so no speculative case exists to discard, and no savepoint is needed.
- **No orphaned reservation.** A reservation without its case cannot commit, because of the deferred foreign key. A failure anywhere before `COMMIT` removes both rows.

Compared with the original sketch in [Case creation semantics](../docs/case-creation-semantics.md#implemented-transaction) (insert the case, then the record), reserve-first removes the losing-case hazard by construction.

**Bounded wait value.** The default is **2000 ms** (`EAISO_IDEMPOTENCY_WAIT_MS`, 1–9000 ms). In 200 sequential keyed creates against PostgreSQL, the whole call (connect, credential lookup, locks, reservation, insert, commit) took median 70.8 ms, p95 104 ms, p99 121 ms, and max 123 ms. Replays took median 64.1 ms and p99 119 ms. The bound is therefore about 16 times the p99, and below the 5 s lock and 10 s statement timeouts. `503` carries no `Retry-After`.

## 6. Sequential and concurrent outcomes

**Sequential (HTTP; database rows asserted):**

| Scenario | Observed |
|---|---|
| First keyed create | `201`, full representation, `Location`, `NEW`/`1`; one case and one record with the exact scope, key, version 1, and expected digest |
| Same key and payload again | `200`, `Location`, `Idempotency-Replayed: true`, body exactly `{"id", "organization_id"}`; no change |
| Same key, changed description | `422` `idempotency_payload_mismatch`; body discloses neither title nor description; no change |
| Different keys with identical content; no key twice | Four distinct cases; records only for the two keyed ones |

**Concurrent** (real sessions; the first request is held at its case `INSERT` by a `SHARE` lock on `case_record`):

| Scenario | Observed |
|---|---|
| Two identical requests | The second waited on the first's uncommitted reservation (`pg_blocking_pids()` named the first backend). First → `201`, second → `200` with the same ID. One case, one record |
| Same key, different payloads | First `201`, second `422`. One case |
| Winner rolls back (first backend terminated while holding its reservation) | First → `500` `internal_error`; the waiting request won the reservation and returned `201`. One case (the second's), one record |
| Wait exceeds the bound (300 ms client) | `503` `idempotency_request_in_flight` after ≥ 0.25 s and < 5 s, with a `request_id`; no case committed. After the first committed, a retry with the same key → `200` with the first's ID |
| Unrelated key while a manual session holds an uncommitted reservation for key K | The other key → `201` in under 1.5 s. Key K waited on the holder; the holder's commit → `200` replay of the holder's case; the holder's rollback → `201` new case |
| Revocation committing first | A keyed replay waited on the uncommitted membership deactivation, then got `403`; no key query was issued |

After every test, no record lacks its case (a join check). Every database test removes its rows afterwards.

## 7. Actor and customer isolation

| Scenario | Observed |
|---|---|
| Three actors in customer A using one key (same payload, and a changed payload) | Three `201`s, three cases, three records; no replay and no `422` across actors (S09) |
| One actor with the same key in A and B | Two `201`s, one record per customer |
| Credential rotation (same actor) | Old credential `401`; new credential → `200` replay with the original ID |
| Revocation before replay (membership, actor, credential) | `403`, `403`, `401` respectively; no `id` or `Location`; **no SQL statement touching `case_creation_idempotency` was issued** (statement recorder) |
| Existing key with a forbidden or nonexistent organization selector | `403`; no key query; no change |
| Requester replay after staff changed title, status, and version | `200` with only `id` and `organization_id`; **no statement touching `case_record` was issued** for the replay; the requester's `GET` still returns `404` (S08) |

## 8. HTTP response contracts

| Outcome | Status | Headers | Body |
|---|---|---|---|
| New keyed case | `201` | `Location: /cases/{id}`, `X-Request-ID` | Full creation representation |
| Replay | `200` | `Location`, `Idempotency-Replayed: true`, `X-Request-ID` | `{"id", "organization_id"}` only (returned as its own response object, so the `201` model cannot add fields to it or reject it) |
| Payload mismatch | `422` | `X-Request-ID` | `idempotency_payload_mismatch`, fixed message |
| Bounded wait expired | `503` | `X-Request-ID` | `idempotency_request_in_flight` ("still in progress. Retry with the same key."), `request_id` |
| Commit outcome unknown (keyed) | `500` | no `Location` | `case_creation_outcome_unknown`: "may or may not have been created. Retry with the same Idempotency-Key and payload to learn the outcome." |
| Commit outcome unknown (unkeyed) | `500` | no `Location` | Unchanged from before: a retry may create a duplicate |
| Malformed key | `400` | — | `invalid_idempotency_key`, fixed message; the value is never echoed |
| No key | as before | — | Distinct cases |

**`503` vs outcome unknown:**
- **`503`:** a different request with the same scope is unresolved; **this** request wrote nothing.
- **Outcome unknown:** **this** request's own `COMMIT` failed, so its case may exist.

`GET /cases/{id}` and `GET /cases` are unchanged, including the interim list shape without cursors, and the full earlier HTTP suite passes.

## 9. Real commit-ambiguity evidence and recovery

| Fault | Kind | Observed |
|---|---|---|
| TCP proxy cuts the connection when `COMMIT` is sent, without forwarding it | **Real** | `500` keyed outcome-unknown; the server rolled back (no case, no record). A retry with the same key → `201`; one case in total |
| Proxy forwards `COMMIT`, sees it succeed, and withholds the reply | **Real** | `500` keyed outcome-unknown; one case and one record exist. A retry with the same key → `200` replay of **that** case, which resolves the ambiguity |
| Backend terminated after the reservation, while waiting at the case `INSERT` | **Real** | `500` `internal_error`; no case and no record (no partial pair). A retry → `201` |
| Driver error on the reservation `INSERT` | Simulated | `500`; nothing persisted; a retry → `201` |
| `COMMIT` raises before it is sent | Simulated | `500` keyed outcome-unknown; nothing persisted; a retry → `201` |

## 10. Negative controls

Each mutation was applied alone, the keyed, parsing, and HTTP test files were run, and the source was restored byte for byte. All ten were caught:

| Mutation | Tests failed |
|---|---|
| Keyed path skips authorization | 4 |
| Reservation without `ON CONFLICT … DO NOTHING` | 12 |
| Expired wait reported as `500` instead of `503` | 2 |
| Reservation wait not bounded (default 5 s lock timeout) | 2 |
| Mismatch treated as replay | 4 |
| Fingerprint ignores the description | 4 |
| `json.dumps(sort_keys=True)` instead of RFC 8785 | 22 |
| Parser accepts tokens as keys | 16 |
| Malformed key falls back to unkeyed creation | 18 |
| Replay returned through the `201` path | 11 |

**A test-robustness fix came out of this run.** On the first run, the "expired wait reported as `500`" mutation made `test_bounded_wait_returns_503_then_retry_resolves` hang. A failed assertion left a client context while a released request was still running. All concurrency tests now resolve their background requests, with a bounded wait, inside `finally` before any client can shut down. With the fix the mutation was caught in 52 s, and the unmutated suite still passes.

## 11. Security and structural boundary

- **No leakage:** across `201`, `200`, `422`, `403`, `503`, and `201` responses (captured at DEBUG), neither the raw key nor its fingerprint appeared in any response body, any header other than `Location`, or any log record. The credential never appeared in logs.
- **Structural check:** `python -m scripts.check_case_access_boundary` reports **0 violations**. Route modules hold no SQL, no authorization logic, and no fingerprinting; they pass the decoded key to the layer, which validates it again.
- **Reviewed checker changes** (the only allowlist changes):
  - `src/request_fingerprint.py` added to `TRUSTED_MODULES`. It is pure (no I/O or SQL), and routes may not import it;
  - `IdempotencyPayloadMismatch` and `IdempotencyRequestInFlight` added to the outcome names routes may import from `src.case_authorization`, like the existing outcome classes;
  - `case_creation_idempotency` added to the protected tables, so route code cannot name it.
- **Route stdlib allowlist:** unchanged. The parser validates base64 syntax itself instead of importing `base64`.

**Independent review.** A separate reviewer examined the implementation, including the READ COMMITTED behavior of the reserve-first transaction, and ran its own parser and serializer checks. It found no high or medium issues. Its low findings were handled as follows:

- the `503` classification was narrowed to the reservation statement alone;
- tests were added for case-sensitive keys, duplicate JSON properties with a key, more malformed-key shapes, and the commit-time foreign-key check;
- the ordering in which key syntax (`400`) precedes authentication (`401`) was kept as designed, because it reveals nothing about stored data.

## 12. Full regression results

| Run | Environment | Executed | Passed | Failed | Skipped | Deselected |
|---|---|---|---|---|---|---|
| Full suite, run 1 | Fresh, privilege opt-in | 528 | 528 | 0 | 0 | 0 |
| Full suite, run 2 | Fresh, opt-in | 528 | 528 | 0 | 0 | 0 |
| Full suite, run 3 | Fresh, opt-in | 528 | 528 | 0 | 0 | 0 |
| PostgreSQL-backed tests only (`-m db`) | Fresh, opt-in | 175 | 175 | 0 | 0 | 353 |
| Offline tests only (`-m "not db"`) | none | 353 | 353 | 0 | 0 | 175 |
| Header and fingerprint conformance (`test_idempotency_parsing.py`) | none | 113 | 113 | 0 | 0 | 0 |
| Keyed PostgreSQL and HTTP tests (`test_keyed_case_creation.py`) | Fresh | 30 | 30 | 0 | 0 | 0 |
| Concurrency and fault-injection subset of the keyed tests | Fresh | 12 | 12 | 0 | 0 | 18 |
| HTTP API tests (`test_http_case_api.py`) | Fresh | 108 | 108 | 0 | 0 | 0 |
| Structural boundary tests | none | 14 | 14 | 0 | 0 | 0 |
| Full suite | Existing environment, **no** privilege opt-in | 524 | 524 | 0 | 4 | 0 |
| Full suite | No `TEST_DATABASE_URL` | 353 | 353 | 0 | 175 | 0 |
| Schema-integrity SQL | Fresh `eaiso_dev` (001–004) | 11 checks | 11 | 0 | — | — |
| `python -m scripts.check_case_access_boundary` | — | — | 0 violations | — | — | — |

**Per module (fresh):**
- identity and grant integrity 87 (61 PostgreSQL-backed);
- credential verification 59;
- centralized authorization 73;
- structural boundary 14;
- readiness 6;
- database privilege 4 (opt-in);
- HTTP API 108;
- **idempotency parsing and fingerprint 113 (new, offline)**;
- **keyed case creation 30 (new, PostgreSQL-backed)**;
- case service 16;
- AI-triage failure paths 14;
- model output 4.

The suite grew from 372 to 528 tests:
- 143 are in the two new modules;
- the HTTP module grew from 95 to 108, because the interim `501` tests were replaced by `Idempotency-Key` syntax tests.

The authorization suite gained no tests, but its public-entry-point and signature tests now include `create_case_keyed`, and its fixtures require migration 004 and clean up idempotency records. After the runs, no test roles, actors, cases, or idempotency records remained in the fresh `eaiso_test`.

Skipped tests are never counted as passed. The four database-privilege tests run only with `EAISO_ALLOW_TEST_ROLES=1` on a disposable cluster.

## 13. Remaining limits

- **Records never expire** (local demonstration, ADR-0004 rule 10). Production needs finite retention, cleanup, resource limits, and post-expiry semantics.
- **Duplicate-safety is scoped.** It covers only retries with the same key, actor, organization, and payload. There is no cross-actor deduplication, and no exactly-once network delivery. Unkeyed requests and the local `create_case()` remain non-idempotent.
- **`503` scope.** A lock wait during the reservation statement maps to `503`. That includes the statement's own foreign-key locks on the organization and actor rows, which do not conflict with ordinary activity.
- **Concurrency coverage is scenario-based.** The tested interleavings and the database constraint are the evidence; no exhaustive race-freedom proof is claimed.
- **Not production:** no production identity, no database-enforced tenant isolation (no row-level security; the development connection may be a superuser), no shared or production deployment, and no status transitions or AI approval over HTTP.
- **Measurement scope:** the wait bound comes from one local measurement (Windows client, local Docker PostgreSQL).

## 14. Reproduction

```bash
pip install -r requirements-api.txt
python -m scripts.check_case_access_boundary                       # 0 violations
python -m pytest tests/test_idempotency_parsing.py -v              # offline conformance

docker run -d --name eaiso-fresh -e POSTGRES_USER=eaiso_fresh -e POSTGRES_PASSWORD=<throwaway> \
  -e POSTGRES_DB=eaiso_dev -p 127.0.0.1:55433:5432 postgres:16
docker exec eaiso-fresh createdb -U eaiso_fresh eaiso_test
for db in eaiso_dev eaiso_test; do
  for f in 001_init.sql 002_case_status_events.sql 003_actor_identity_and_grants.sql 004_case_creation_idempotency.sql; do
    docker exec -i eaiso-fresh psql -q -v ON_ERROR_STOP=1 -U eaiso_fresh -d $db < db/$f
  done
done
docker exec -i eaiso-fresh psql -q -v ON_ERROR_STOP=1 -U eaiso_fresh -d eaiso_dev < db/seed_dev.sql
docker exec -i eaiso-fresh psql -v ON_ERROR_STOP=1 -U eaiso_fresh -d eaiso_dev < tests/test_schema_integrity.sql

export TEST_DATABASE_URL="postgresql://eaiso_fresh:<throwaway>@127.0.0.1:55433/eaiso_test"
python -m pytest tests/test_keyed_case_creation.py -v              # PostgreSQL + HTTP keyed tests
EAISO_ALLOW_TEST_ROLES=1 python -m pytest tests -q -rs               # full suite (disposable cluster only)
docker rm -f eaiso-fresh
```

Existing local databases need `db/004_case_creation_idempotency.sql` applied after 001–003. The database test fixtures stop with a clear message if it is missing.
