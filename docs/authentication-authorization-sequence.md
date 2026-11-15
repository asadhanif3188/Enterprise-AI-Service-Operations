# Server-Side Authentication and Authorization Sequence

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** **Partly implemented.** The architecture is **accepted** in [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) (2026-11-15). **Implemented:**
- the `actor` and `organization_membership` tables ([`db/003_actor_identity_and_grants.sql`](../db/003_actor_identity_and_grants.sql));
- the local credential resolver ([`src/credential_auth.py`](../src/credential_auth.py); [section 5.1](#51-implemented-local-credential-resolver));
- the authorization policy ([`src/case_authorization.py`](../src/case_authorization.py));
- the authorized-case-operations layer ([`src/authorized_case_operations.py`](../src/authorized_case_operations.py); [section 7.1](#71-implemented-authorized-case-operations));
- the structural no-bypass check ([`scripts/check_case_access_boundary.py`](../scripts/check_case_access_boundary.py));
- the localhost-only HTTP API ([`src/api/`](../src/api/); [section 7.2](#72-implemented-local-http-api)), with HTTP tenant-isolation tests against PostgreSQL and server-generated request IDs.

**Not implemented:** keyed idempotent creation (ADR-0004), cursor pagination, HTTP status transitions, production authentication, and database-enforced tenant isolation.


**Scope:** The request-time sequence for `POST /cases`, `GET /cases/{id}`, and `GET /cases`. It describes **how** the [minimum role-permission policy](authorization-permissions.md) is to be enforced, as accepted in ADR-0005. It does not implement anything.  
**Evidence class:** Architecture design, except sections 5.1 and 7.1, which describe implemented code with recorded test results ([credential verification results](../tests/credential-verification-results.md), [case authorization results](../tests/case-authorization-results.md)). The other diagrams, pseudocode, and test cases are proposals, not observed results.

## 1. Baseline

**What exists:**

- `organization`, `case_record`, and `ai_suggestion` are created by [`db/001_init.sql`](../db/001_init.sql); `case_status_event` by [`db/002_case_status_events.sql`](../db/002_case_status_events.sql).
- [`src/case_service.py`](../src/case_service.py) filters queries by a **caller-supplied** `organization_id`. That is query scoping, not authorization.
- The passing test `test_wrong_organization_scope_cannot_read_or_update` ([results](../tests/case-operations-results.md)) shows only that filtering. It is not evidence of authenticated tenant isolation.
- `actor` and `organization_membership` exist, with role-grant eligibility guards ([`db/003_actor_identity_and_grants.sql`](../db/003_actor_identity_and_grants.sql)).
- A local credential resolver ([`src/credential_auth.py`](../src/credential_auth.py)) verifies opaque test credentials and returns an `actor.id`, or a typed failure. The authorized-case-operations layer calls it.
- No row-level security is enabled. A foreign key proves that a referenced organization exists. It never proves that a caller is a member of it.

**Design inputs:**

- The [API contract](api-contract.md): endpoints and error outcomes.
- The [identity model](identity-model.md): `actor`, and `organization_membership` keyed by `(actor_id, organization_id)`, both implemented as schema.
- The [permission policy](authorization-permissions.md): permissions, matrix, predicates, and test vectors V01–V22.

**Governing decisions:**

- [ADR-0001](adr/0001-postgresql-authoritative-case-store.md): PostgreSQL is authoritative but provides no tenant authorization by itself.
- [ADR-0002](adr/0002-ai-suggestions-non-authoritative.md): AI output and case text never confer permissions.
- [ADR-0003](adr/0003-application-controlled-case-transitions.md): `transition_case()` must not be exposed to untrusted callers.
- [ADR-0004](adr/0004-keyed-idempotent-case-creation.md): **Accepted** (2026-11-15) as design, **not implemented**. Its key scope is the *authorized* organization plus the *verified* actor.
- [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md): **Accepted** (2026-11-15). Implemented in Python and exposed through the local HTTP API. It records this sequence's identity, enforcement, and revocation decisions.

**Policy summary.** All rows below assume an active actor with an active membership in the organization being evaluated. Any other state is denied.

| Role | Create | Read | List |
|---|---|---|---|
| `service_engineer` | Allow | Allow | Allow |
| `service_manager` | Allow | Allow | Allow |
| `requester` | Allow, create-only in its own member organization (accepted in ADR-0005) | Deny | Deny |

`platform_administrator` has no implicit access. Unknown roles have no permissions.

## 2. Separate concerns

| Concern | Input | Output | Trusts client-supplied IDs? |
|---|---|---|---|
| **Authentication** | `Authorization: Bearer <opaque test credential>` | A verified `actor.id`, or rejection | No |
| **Principal status** | Verified `actor.id` | `actor.is_active` confirmed | No |
| **Authorization** | Verified actor, action, target organization | Allow, as a server-created scope, or deny | The target organization is a **selector** only |
| **Enforcement** | The server-created scope and a validated request | A case operation restricted to the authorized organization | No |

The client may **request** a target organization. It cannot tell the server who it is, which organizations it belongs to, what role it holds, or who owns a case.

## 3. Component responsibilities

These components are logical, and all of them now exist; request logging is still partial. They can be modules in one small Python application; they do not need to be separate services.

| Component | Responsibility | Must not |
|---|---|---|
| **HTTP boundary** (IMPLEMENTED: [`src/api/`](../src/api/), localhost only) | Parse and validate the route, headers, and body shape. Pass the raw credential to the authorized layer, which calls the resolver. Map outcomes to HTTP responses. | Infer identity or permission from URLs, headers, or body fields. Call `src/case_service.py`, or issue SQL against case tables, directly. |
| **Credential resolver** (IMPLEMENTED: [`src/credential_auth.py`](../src/credential_auth.py)) | Verify an opaque, local, test-only credential against server-controlled configuration and return an `actor.id`. | Accept a display name, client-supplied actor ID, unverified token claims, or tenant headers as identity. |
| **Authorization policy** (IMPLEMENTED: [`src/case_authorization.py`](../src/case_authorization.py)) | Load the actor, the membership, and the persisted case owner (for reads). Evaluate the exact permission. Return either an authorized scope or a denial. | Allow unknown actions or unknown roles, or provide an administrator bypass. |
| **Authorized case operations**, the enforcement layer (IMPLEMENTED: [`src/authorized_case_operations.py`](../src/authorized_case_operations.py); it runs its own transactions and does not call the case service) | Be the **only** path from routes to case data. Call the policy itself. Run the operation restricted to the authorized organization. Return only the permitted fields. | Accept a raw organization or actor ID from the route as authority. |
| **PostgreSQL** (EXISTING, including actor and membership tables) | Authoritative case data, and later actors and memberships. | Be treated as an authorization mechanism because foreign keys exist. |
| **Request logging** (PARTLY: the layer logs decision categories and actor IDs; the HTTP boundary logs method, path, status, and a server-generated request ID, which server errors also return. Layer log lines do not yet carry the request ID) | Record a correlation ID, the action, the verified actor reference, the decision, and a sanitized error category. | Log credentials or case text, or record client headers as verified identity. |

**Which component enforces the decision?** The authorized-case-operations layer. A route hands that layer the caller's opaque credential and the request; the layer resolves the credential itself, so a route cannot supply an actor ID, and then **calls the policy itself**. A route therefore cannot perform a case operation while skipping the decision.

Python cannot make the lower-level functions unreachable. The rule that routes never import `src/case_service.py` directly is therefore backed by code review and an automated structural check, implemented as an AST scan in [`scripts/check_case_access_boundary.py`](../scripts/check_case_access_boundary.py) (section 7.1). The authorized scope is a value created by the server, not a cryptographic capability.

## 4. Logical sequence

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant API as HTTP boundary (IMPLEMENTED, localhost)
    participant AN as Credential resolver (IMPLEMENTED)
    participant OPS as Authorized case operations (IMPLEMENTED)
    participant POL as Authorization policy (IMPLEMENTED)
    participant DB as PostgreSQL (EXISTING)
    C->>API: Request, bearer credential, target selector
    API->>AN: Verify opaque credential
    alt Missing, invalid, or unknown credential
        AN-->>API: No actor
        API-->>C: 401, no data
    else Verified
        AN-->>API: Verified actor id
        API->>OPS: Actor id, action, validated request
        OPS->>POL: authorize(actor id, action, target)
        POL->>DB: Actor, membership, persisted case owner for reads
        alt Denied, or case not found
            POL-->>OPS: Denial
            OPS-->>API: Denial category, no case data
            API-->>C: 403 (including inactive actor) or generic 404
        else Allowed
            POL-->>OPS: Authorized scope
            OPS->>DB: Operation restricted to the authorized organization
            DB-->>OPS: Rows or commit result
            OPS-->>API: Permitted fields only
            API-->>C: 200 or 201
        end
    end
```

The diagram shows **logical order**. It does not claim that a separate policy query followed by a separate operation is free of races (section 8). For reads and lists, the policy check and the operation may be merged into one scoped query. A database failure at any step gives a generic `500` and no protected operation (section 7).

## 5. From credential to actor

**How the API determines a trusted `actor.id`:**

1. A **local, test-only, opaque** bearer credential is generated independently of `actor.id`.
2. The server maps that credential to an `actor.id` using configuration the client cannot read or edit. No usable credential value is committed to the repository.
3. An unknown credential is rejected with `401`. There is **no fallback** to `X-Actor-Id`, `X-User-Id`, `X-Org-Id`, query parameters, body fields, or `display_name`.

**Why knowing an actor UUID or display name is not enough:**

- A UUID is an identifier, not a secret. It can appear in responses, logs, or URLs, and being hard to guess is not authentication.
- `display_name` is not unique, not secret, and not bound to any credential ([identity model §3](identity-model.md#3-actor)).
- Only possession of a credential that the server verifies yields an actor ID. Even then, the actor ID alone authorizes nothing: the active actor, the membership, the role, and the target must all still be checked.

**What this is not:** production authentication, OIDC, JWT signature verification, SSO, or session management. Until real authentication is designed, the demonstration API must stay local or test-only. Credential and lookup-key values must never be logged.

**Accepted in ADR-0005:**

- Credentials are generated with a cryptographically secure random generator.
- Revocation removes or disables the credential mapping. Rotation replaces the credential and keeps the same `actor.id`.
- A missing, invalid, unknown, or revoked credential gets `401`. So does a credential that maps to an actor ID with no `actor` row.
- A valid credential for an inactive actor gets `403`.

**Left to implementation:** the configuration mechanism and the comparison method, for example a hashed lookup or a constant-time comparison. Section 5.1 records the choice.

### 5.1 Implemented local credential resolver

[`src/credential_auth.py`](../src/credential_auth.py) implements the resolver; [`scripts/manage_test_credentials.py`](../scripts/manage_test_credentials.py) is the operator tool. Observed results are in the [credential verification results](../tests/credential-verification-results.md). It establishes **identity only**: it does not read memberships, roles, or organizations, and no HTTP route calls it yet.

**Credential format.** `eaiso_test_` followed by `secrets.token_urlsafe(32)`: 256 random bits as 43 base64url characters. The prefix makes test credentials recognizable to secret scanners. Anything that is not exactly this canonical form is rejected as malformed before any lookup.

**Server-controlled configuration.** A local JSON store, by default `.local/test-credentials.json` (git-ignored; override with `EAISO_CREDENTIALS_FILE`). Each entry holds a random `credential_id`, the SHA-256 digest of the credential, the `actor_id`, a status (`active` or `revoked`), and timestamps. **Raw credentials are never stored**: the operator tool prints a new credential once and keeps only its digest. The store is validated strictly (unknown keys, malformed digests or UUIDs, and duplicate digests or IDs are rejected). Writers take an exclusive lock file, so concurrent operator runs cannot lose an update or re-enable a revoked credential, and replace the file atomically, so readers see either the old or the new store.

**Comparison.** The presented credential is hashed with SHA-256 and the digest is looked up in the store. With 256-bit random secrets, an unsalted fast hash is sufficient: the digest cannot be reversed or brute-forced, and lookup timing can reveal at most information about digests, which does not help recover a credential. No secret-dependent string comparison is made on raw values.

**Resolution order and outcomes** (`status_equivalent` is the ADR-0005 status; the HTTP boundary returns it, see [section 7.2](#72-implemented-local-http-api)):

| Condition | Outcome | `status_equivalent` |
|---|---|---|
| Missing (`None` or empty) | `Unauthenticated("missing")` | 401 |
| Not the canonical format, including actor IDs, display names, digests, or `Bearer …` strings | `Unauthenticated("malformed")` | 401 |
| Well-formed but not in the store | `Unauthenticated("unknown")` | 401 |
| Mapping revoked | `Unauthenticated("revoked")` | 401 |
| Mapping names an actor with no `actor` row | `Unauthenticated("actor_not_found")` | 401 |
| Actor exists but `is_active = FALSE` | `InactiveActor` | 403 |
| Store missing, unreadable, or invalid | `AuthenticationUnavailable("configuration")` | 500 |
| Database unreachable, timed out, or erroring | `AuthenticationUnavailable("database")` | 500 |
| Active mapping, active actor | `VerifiedActor(actor_id, credential_id)` | — |

The actor check is one read-only statement (`SELECT is_active FROM actor WHERE id = …`) with bounded connect and statement time. A later authorization step must still repeat the actor predicate inside its own scoped statement or transaction (section 8).

**Lifecycle.** *Revocation* marks a mapping `revoked`, or removes it. *Rotation* issues a new credential for the same `actor_id` and revokes the old one in a single atomic store write. The store is re-read on every resolution and re-parsed when its content changes, so both take effect on the next call without a restart. If the store later becomes unreadable or invalid, resolution fails closed instead of using the previous mapping.

**Logging.** Records carry only the outcome category, the non-secret `credential_id`, and the verified `actor_id`. Raw credentials and digests never appear in logs or exception messages. Dependency exceptions are not chained into authentication errors (neither `__cause__` nor `__context__`), so a driver message that echoed a secret cannot leak through an error reporter that walks the chain.

**At the HTTP boundary.** Every `Unauthenticated` reason produces the **same** `401` response (implemented and tested); the `reason` is for server-side logs only, so a client cannot tell an unknown credential from a revoked one.

**Limitations.**

- The store is a local file. Anyone who can write it, or the database, controls which actor a credential names. Owner-only permissions are applied on POSIX only, and the default path is relative to the working directory, so the resolver must be started from the repository root or given `EAISO_CREDENTIALS_FILE`.
- A lock file left by a killed operator run blocks later writes until it is removed by hand. On Windows, a store being replaced while it is read can make one resolution fail closed (`AuthenticationUnavailable`); writers retry a refused replacement briefly.
- There is no expiry, rate limiting, lockout, or audit trail of credential use, and no production credential management, OIDC, or SSO.
- Bearer credentials are replayable by whoever holds them. The local API serves plain HTTP on the loopback interface only; it has no TLS and is not for use beyond the local machine.
- Authentication says nothing about authorization: an actor with no grants still authenticates.

## 6. Per-endpoint decision flows

The three flows below are implemented: the HTTP steps in [`src/api/`](../src/api/) (section 7.2), and the authorization and SQL steps in the authorized-case-operations layer (section 7.1). The step order shown is logical; the exact implemented order of request parsing and authentication is in section 7.2.

### `POST /cases` (`case:create`)

1. Authenticate (`401` on failure).
2. Validate the body. Reject unexpected fields such as `id`, `actor_id`, `role`, `status`, or `version`; they are **not silently ignored** ([API contract §5](api-contract.md#5-post-cases--create-a-case-implemented-unkeyed-only)). The body's `organization_id` is an **untrusted selector**.
3. Check that the actor is active (`403` if not).
4. Look up the membership for `(actor, requested org)`. It must be active, and its role must grant `case:create`. Otherwise return `403` with no case data.

   A nonexistent organization has no membership, so it gets the same `403` as a foreign organization. The response does not reveal whether the organization exists.
5. Create the case using the **authorized** organization from the scope, not the raw body value. It starts at `NEW`, version `1`, with no AI call. Return `201` only after commit.
6. **Keyed creation, when enabled** ([ADR-0004](adr/0004-keyed-idempotent-case-creation.md), Accepted design; disabled until ADR-0004 and ADR-0005 are implemented and tested):
   - Authorize first, before any replay lookup.
   - Scope the key as `(authorized organization_id, verified actor_id, 'case:create', key)`.
   - A replay returns `200` with identity only (`id`, `organization_id`), never the current case state (section 9).

**Isolation:** the selector can only choose one of the actor's own active memberships. Any other organization is denied before anything is persisted.

### `GET /cases/{id}` (`case:read`)

1. Authenticate.
2. Parse `{id}` as a UUID. A malformed value is rejected by validation. The ID is a lookup key, not a permission.
3. Check that the actor is active.
4. The organization to evaluate is the case's **persisted** `case_record.organization_id`. A client-supplied organization never asserts ownership.

   Preferred shape: a single query that joins the case to an active membership for the verified actor with a role granting `case:read`, so that no unscoped row is fetched.
5. If no row comes back, return the **same generic `404` status and body**, whether the case does not exist, belongs to another organization, or the actor lacks `case:read`. That includes a requester asking for a case it just submitted.

   This reduces disclosure of whether a case exists. It does not claim to eliminate timing side channels.
6. Return only the permitted fields.

**Isolation:** ownership comes from the database, never from the request. An actor with membership only in B cannot read A's cases by guessing UUIDs.

### `GET /cases?organization_id=…` (`case:list`)

1. Authenticate.
2. Require a valid `organization_id` selector. If it is missing or invalid, reject the request as invalid (implemented as `400`). Never fall back to listing all organizations.
3. Check that the actor is active, and that it has an active membership in the selected organization with a role granting `case:list`. Otherwise return `403` with no rows.
4. Apply the authorized organization predicate to **every** list query, including any cursor continuation. Bound the page size. A cursor is bound to the authorized organization and can never switch tenants.
5. Return only the list projection.

**Isolation:** one authorized organization per request. An actor with mixed roles, for example manager in A and requester in B, can list A but gets `403` for B (vector V12).

**Unsupported operations:** there is no HTTP route for case updates, status transitions, AI approval, actor or membership management, or platform-administrator access. An unknown route never dispatches to `transition_case()`. Its absence is not evidence that update authorization exists.

## 7. Policy interface and failure handling

```text
# Conceptual interface only; not source code
resolve_actor(opaque_credential)                         -> ActorId | AuthenticationFailure
authorize(actor_id, action, target)                      -> AuthorizedScope | Denial(category)
run_authorized(scope: AuthorizedScope, validated_request) -> Result      # the only route-facing case API

authorize:
  action not in {case:create, case:read, case:list}  -> Denial(unsupported)
  actor not active                                    -> Denial(inactive_actor)   # 403
  # a credential mapped to a missing actor row is treated as unresolved -> 401
  org := persisted case owner                         if action = case:read
         validated request organization_id (selector) otherwise
  case:read and case not found                        -> Denial(not_found)        # 404
  membership(actor_id, org) missing or not active     -> Denial(forbidden or not_found)
  action not in PERMISSIONS[membership.role]          -> Denial(forbidden or not_found)  # unknown role => {}
  -> AuthorizedScope(actor_id, org, action)
```

These predicates are the same as [policy §4](authorization-permissions.md#4-policy-predicates). For `case:read`, every denial maps to the generic `404`. For create and list, it maps to `403`.

**Failure outcomes:**

| Situation | Outcome |
|---|---|
| Missing, invalid, unknown, or revoked bearer credential, or one that maps to no existing actor | `401`; no lookup of any client-supplied identity |
| Valid credential, inactive actor | `403`, always denied (ADR-0005) |
| No membership, inactive membership, or role lacks the permission, on create or list | `403`, no rows, no membership details |
| Unknown role value | Denied; treated as having no permissions |
| Unknown, foreign, or unreadable case on `GET /cases/{id}` | Identical generic `404` status and body |
| Database outage or policy-evaluation error | **Fail closed:** no protected operation. Generic `500` with a correlation ID, per the [API contract error table](api-contract.md#8-proposed-error-contract). Never a fabricated `403` or `404`, and never a fallback to unscoped SQL. |

**Logging:** record a correlation ID, the verified actor reference where appropriate, the action, the decision, and a sanitized category. Never log credentials, case descriptions, or untrusted headers as identity. Audit and attribution requirements remain **OPEN** ([policy §8](authorization-permissions.md#8-open-issues)).

### 7.1 Implemented authorized case operations

[`src/authorized_case_operations.py`](../src/authorized_case_operations.py) is the single route-facing entry point. The permission matrix lives in [`src/case_authorization.py`](../src/case_authorization.py) and is the only source of the role predicates used in SQL. Observed results: [case authorization results](../tests/case-authorization-results.md).

**Entry points.** Each takes the caller's **opaque credential**, never an actor ID, and resolves it with the credential resolver:

- `create_case(credential, organization_id, title, description)`;
- `get_case(credential, case_id)`;
- `list_cases(credential, organization_id, limit=20)`.

There is no parameter for an actor, a decision, or an authorized scope. A constructed `VerifiedActor`, an actor UUID, or a display name passed as the credential is rejected as malformed.

**Create.** The organization is an untrusted selector. Create runs one READ COMMITTED transaction:

1. `SELECT is_active FROM actor … FOR SHARE`;
2. in a separate statement, `SELECT … FROM organization_membership … FOR SHARE`;
3. the policy check;
4. the `INSERT` into the membership's organization;
5. `COMMIT`.

Both row locks are held until the commit, and the representation is returned only after it. A missing, inactive, or non-permitting membership, or a nonexistent organization, raises `OrganizationAccessDenied` with no insert. It is not idempotent: there is no key, and separate calls create separate cases. The existing `create_case()` in `src/case_service.py` is not reused, because calling it after a check would release the locks first.

**Read.** An actor-status check, then **one** statement. The statement joins the case to an active membership in the case's **persisted** organization, with a permitted role and an active actor. Missing, foreign, and unreadable cases raise the same `CaseNotFound`.

**List.** One statement computes the actor status and the authorization decision for the selected organization. Case rows are produced only when that decision is true, and only for that organization, ordered by `(created_at, id)`. The limit defaults to 20, with a maximum of 100, and the result has a `has_more` flag. **Cursors are deferred to the HTTP increment**, so there is no cursor that could widen scope.

**Outcomes** (typed exceptions carrying the ADR-0005 status; the HTTP mapping is in section 7.2):

| Situation | Outcome | Status equivalent |
|---|---|---|
| Credential missing, malformed, unknown, revoked, or for a missing actor | `Unauthenticated` (credential resolver) | 401 |
| Actor inactive, at resolution or in the layer's own check | `InactiveActor` | 403 |
| Create or list in an organization without an active, permitting membership, including a nonexistent one | `OrganizationAccessDenied` | 403 |
| Missing, foreign, or unreadable case | `CaseNotFound` (identical in every case) | 404 |
| Non-canonical or malformed UUID, blank text, a NUL character or other text that cannot be encoded as UTF-8 (such as a lone surrogate), text over 10,000 characters, or an out-of-range limit (checked after authentication, before any lock) | `InvalidCaseRequest` | 400 |
| Database unreachable, lock timeout, statement timeout, deadlock, or any other failure before commit, including unexpected non-driver errors | `CaseOperationUnavailable`, nothing written | 500 |
| Failure during `COMMIT`, including a real connection loss | `CaseCreationOutcomeUnknown`, never reported as success or rollback | 500 |

**Construction.** Route code obtains the layer only from `operations_from_environment(dsn)`, which wires the real credential resolver from server-side configuration. Routes cannot import the layer class, a resolver, the credential store, or credential-management functions, so they cannot choose the caller's identity.

**Structural no-bypass check.** [`scripts/check_case_access_boundary.py`](../scripts/check_case_access_boundary.py) is run by the test suite. It was hardened after the [security-readiness review](../tests/authorization-boundary-readiness-results.md).

- **Restricted modules:** every module under `src/` that is not in the checker's explicit trusted list. Future HTTP handlers belong in `src/api/`, but the rules apply wherever a new `src/` module is created. Trusting a new module is a reviewed change. Restricted modules:
  - may import **only**:
    - named outcome classes, action constants, and the layer factory from the trusted modules, never a whole trusted module;
    - other restricted `src` modules, which are checked by the same rules;
    - FastAPI, Starlette, Pydantic, and `typing_extensions`, minus string-import helpers (`ImportString`, `pydantic.v1`) and private submodules;
    - a small standard-library allowlist (`typing`, `uuid`, `datetime`, `dataclasses`, `enum`, `re`, `json`, `logging`, `functools`, `time`, `http.HTTPStatus`, and a few others). `sys`, `os`, `operator`, `inspect`, `gc`, `importlib`, process, and network modules are excluded, including when reached through an allowed module (`logging.sys`, `uuid.os`);
  - may not use reflection or dynamic code, including `getattr`, `vars`, `type`, `open`, `attrgetter`, `getattr_static`, `get_type_hints`, and string-import helpers, whether called directly or through an alias such as `m = __import__`;
  - may not access other objects' private attributes (`obj._name`; a class's own `self._x` is allowed) or reflective dunder attributes (`__dict__`, `__class__`, `__getattribute__`, `__globals__`, and similar);
  - may not contain string or bytes literals naming protected tables or the eligibility view as whole identifiers (a REST path such as `/case_records` is not flagged).
- **Everywhere else in the repository** (scripts, trusted modules, and any other Python file outside `tests/` and local environments; every directory under `src/` is scanned, whatever its name):
  - web frameworks may be imported only inside `src/api/`;
  - nothing may import `src.case_service`;
  - protected-table SQL is allowed only in an explicit allowlist of existing modules.
- **Fails closed:** a missing `src/` directory or an unparseable file is a violation, and the default scan root is the repository, not the working directory.
- **Out of reach:** static analysis cannot make arbitrary Python safe. Privileged SQL clients, tests, SQL assembled at runtime, code in string annotations or format strings evaluated by libraries, the internals of allowlisted packages, and trusted modules misusing their access are not detected.

**Not instantaneous revocation.** A read or list statement that started before a revocation committed completes under its snapshot. Reads take no row locks, so they do not wait for an uncommitted revocation.

**Credential revocation timing (observed).** The credential is resolved once, before the database work starts, and the local credential store is not locked with the database transaction. Revoking or rotating a credential therefore stops only later resolutions: an operation already past resolution, including a create waiting inside its locked transaction, completes. Actor deactivation and membership revocation are the controls that serialize with an in-flight create through the row locks. See the [security-readiness results](../tests/authorization-boundary-readiness-results.md).

### 7.2 Implemented local HTTP API

[`src/api/`](../src/api/) is a FastAPI application with exactly three routes, run on `127.0.0.1` only (`python -m scripts.run_local_api`). Observed results: [HTTP case API results](../tests/http-case-api-results.md). Interim behavior and its relation to the contract: [API contract §3.1](api-contract.md#31-implemented-interim-behavior).

**Construction.** At startup the application calls `operations_from_environment()` with no arguments, so the database URL (`DATABASE_URL`) and the credential store (`EAISO_CREDENTIALS_FILE`) come only from server configuration. Missing or invalid configuration stops startup. Routes receive this one instance; they never build a resolver, accept connection details, or hold a policy.

**Request processing order.** FastAPI resolves a route's dependencies in declaration order, before the route body:

1. `POST /cases` only: any `Idempotency-Key` header gets `501` (`idempotency_not_implemented`); nothing else is evaluated and nothing is created.
2. `Authorization` header **syntax**: exactly one `Bearer <token>` value, else the generic `401`. The token is not verified here.
3. Request **syntax**: query parameters, then for `POST` the content type, JSON, and body schema (`400`, `415`, `413`, `422`).
4. The authorized layer: **credential verification** (`401`, `403`, `500`), then **value checks** (canonical UUIDs, text, `limit` range), then authorization and the scoped operation.

So the Python layer's authentication-first order holds for value checks, but **request-syntax errors are reported before the credential is verified**: a malformed body with a well-formed but invalid credential gets `400`/`415`/`422`, not `401`. Tests demonstrate both directions. Syntax errors reveal nothing about tenants, cases, or credentials.

**Outcome mapping:**

| Outcome | HTTP | Body `code` |
|---|---|---|
| Every `Unauthenticated` reason, and a missing, duplicated, or malformed `Authorization` header | `401`, `WWW-Authenticate: Bearer`, one identical body | `unauthenticated` |
| `InactiveActor` | `403` | `actor_inactive` |
| `OrganizationAccessDenied` (including a nonexistent organization) | `403` | `organization_access_denied` |
| `CaseNotFound` (missing, foreign, or unreadable, including a requester's own case) | `404`, one identical body | `case_not_found` |
| Request syntax (parameters, JSON) / path and query values after authentication | `400` | `invalid_parameter`, `missing_parameter`, `unknown_parameter`, `duplicate_parameter`, `cursor_not_supported`, `malformed_json` |
| Body schema / body values after authentication | `422` | `invalid_request` |
| `CaseOperationUnavailable`, `AuthenticationUnavailable`, unexpected errors | `500`, with `request_id` | `internal_error` |
| `CaseCreationOutcomeUnknown` | `500`, with `request_id`; never `201` | `case_creation_outcome_unknown` |

Error bodies contain only fixed, server-written text: no exception messages, SQL, membership details, case text, credentials, or client-supplied values. Unknown routes and methods get `404`/`405` in the same envelope.

## 8. Check/use timing and revocation

**Why it races.** If the membership check and the case operation are separate statements, a revocation can commit between them. The operation then runs on an allow decision that is no longer true.

A single statement does not fully close the gap either. Each PostgreSQL statement reads from a snapshot taken when it starts, so a revocation that commits while the statement runs may not be visible to it. **This design does not promise immediate revocation of in-flight requests.**

| Option | Benefit | Limitation |
|---|---|---|
| **A. Separate policy query, then the case operation** | Simplest code | Classic check/use race. An allow decision can be applied after the revocation has committed. |
| **B. Membership predicate inside the case statement**, for example a joined read or `INSERT … SELECT … WHERE EXISTS (active membership)` | Binds authorization and data to one snapshot. A good fit for reads and lists. | A plain statement does not wait for a revocation still in progress. One that commits after the snapshot does not affect the statement. |
| **C. Short transaction: lock the actor and membership rows `FOR SHARE`, then write, then commit** | A concurrent revocation (`UPDATE … SET is_active = false` or a role change) **waits** until the transaction commits. Under READ COMMITTED, a `FOR SHARE` read that meets a concurrent update waits for it, then re-checks its `WHERE` clause against the updated row, so a revocation that commits first is seen. | Lock contention. Deadlock risk if revokers lock rows in a different order; PostgreSQL detects deadlocks and aborts one transaction. Under REPEATABLE READ, the same conflict raises a serialization error that must be handled. Requests that already hold the lock still finish before the revocation. |

**`FOR SHARE` versus `FOR KEY SHARE`.** In PostgreSQL 16, `FOR SHARE` blocks `UPDATE` and `DELETE` of the locked row. `FOR KEY SHARE` blocks only `DELETE`, and updates that change key columns. Updates to non-key columns such as `is_active` or `role` are **not** blocked by it ([explicit locking](https://www.postgresql.org/docs/16/explicit-locking.html), [transaction isolation](https://www.postgresql.org/docs/16/transaction-iso.html)). `FOR KEY SHARE` must therefore not be used if the goal is to hold off a revocation.

**Read-side limit.** Once a read response has been sent, no revocation can take it back. Revocation can only affect requests that have not yet been authorized or committed.

**Accepted minimum contract ([ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md), 2026-11-15).** This is not instant revocation.

- **Reads and lists use option B.** One scoped statement carries the actor and membership predicates. For reads, a preceding actor-status check yields `403` for an inactive actor. A request that began before a revocation committed may complete under its snapshot.
- **Writes use option C, under READ COMMITTED:**
  1. lock the actor row `FOR SHARE`, then the membership row `FOR SHARE`, in **two separate statements**, since one joined locking statement does not guarantee lock order;
  2. hold both locks through the write and the commit.

  A revocation that commits first is seen, and the request is denied. A request that took its locks first may finish while the revocation waits.
- **Keyed creation.** The idempotency lookup and reservation run inside this locked transaction.
- **No caching.** Authorization decisions are not cached across requests.

**Still implementation concerns that need executed tests:**

- lock timeouts and bounded waits;
- deadlock handling (revocations that touch both rows should lock actor, then membership);
- error mapping;
- contention.

The contract's behavior under concurrency is **untested**. Never hold locks across model calls, long-running work, or client I/O.

## 9. Requester create-only and idempotent replay

**A later GET.** A requester's `201 Created` response returns the case as part of the create operation. That is not a `case:read` grant. A later `GET /cases/{id}`, including on the `Location` URL, returns the generic `404` (vector V06). Viewing a case's status is deferred until requester ownership is modeled.

**Replay.** [ADR-0004](adr/0004-keyed-idempotent-case-creation.md) and [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md), both **Accepted** as design, define the following contract. None of it is implemented.

1. **Authorization runs before the replay lookup** on every replay. An actor whose actor record or membership is inactive is denied, and the response says nothing about stored keys.
2. **Identity-only replay.** A replay returns `200 OK` with `Location`, `Idempotency-Replayed: true`, and only `id` and `organization_id`, taken from the idempotency record. It never contains the current status, text, or version, so a create-only actor gains no `case:read` ability and sees no later changes.
3. **The key scope includes the actor.** The scope is `(authorized organization_id, verified actor_id, 'case:create', key)`. Another member who reuses or guesses the same literal key gets an independent operation. That member can neither replay the first actor's case nor cause a `422` `idempotency_payload_mismatch` that reveals the key exists. This closes the cross-actor risk recorded in the previous version of this section.

   The deliberate tradeoff is that two actors who submit one incident are not deduplicated.

**Still pending:** implementation of both ADRs, and executed tests (S08, S09, and the ADR-0004 list). Until then this is an accepted design intention, not a guarantee.

## 10. Future negative test cases

These **complement** the policy's vectors V01–V22 ([policy §6](authorization-permissions.md#6-proposed-test-vectors)) with checks specific to the sequence. Statuses refer to **HTTP** tests through the local API against PostgreSQL ([HTTP results](../tests/http-case-api-results.md)), each asserting the response and the database state. Python-layer equivalents are in the [case authorization results](../tests/case-authorization-results.md). S08 and S09 need ADR-0004.

| ID | Scenario | Expected outcome | Status |
|---|---|---|---|
| S01 | Valid credential mapped to an actor ID that does not exist | `401`; no case data; no insert | Passed (HTTP) |
| S02 | `POST /cases` naming a nonexistent organization, from an actor active in A | `403`, identical to a foreign organization; no insert | Passed (HTTP) |
| S03 | `POST /cases` body containing `actor_id`, `status`, or `version` | Rejected as invalid; no insert | Passed (HTTP, `422`) |
| S04 | A route attempts to reach case data without going through the authorized-operations layer | The structural check fails the build or review | Passed (structural check over `src/api/`; bypass regression tests) |
| S05 | Database unavailable during the policy lookup | Generic `500`; no protected operation; not `403` or `404` | Passed (HTTP) |
| S06 | List cursor issued for A, replayed with `organization_id=B` by an actor in A only | `403` or cursor rejected; no B rows | Interim: no cursor is issued, and any `cursor` parameter is rejected with `400` (HTTP-tested). Cursor scoping itself is not implemented |
| S07 | Membership or actor revoked concurrently with a write, under the accepted write-locking contract | A revocation that commits first leads to denial. An operation holding locks first completes while the revocation waits. Observed ordering recorded. | Passed (HTTP: revocation-first; both orders at the Python layer) |
| S08 | Requester replays its own create key after the case changed | `200` replay with only `id` and `organization_id`; no current state disclosed | Not implemented (keyed requests get `501`) |
| S09 | A second requester in the same organization reuses the first requester's key, with the same and with a different payload | Independent operation: a new case for the second requester; no replay of, and no `422` mismatch about, the first requester's case | Not implemented (keyed requests get `501`) |
| S10 | Logs after a `401`, a `403`, and a `404` | No credential value, no case text, no client header recorded as identity | Passed (HTTP) |

## 11. Review questions

1. **How is a trusted `actor.id` determined?** Only from a server-verified opaque credential, mapped through server-controlled configuration. A UUID or display name is neither secret nor bound to a credential (section 5).
2. **How are Org A and Org B kept apart?**
   - **Create:** the selector must match an active membership with `case:create`.
   - **Get by ID:** the persisted owner must match an active membership with `case:read`, otherwise a generic `404`.
   - **List:** the selector must match an active membership with `case:list`, and every query is scoped to that organization (section 6).
3. **What enforces the decision?** The authorized-case-operations layer, which calls the policy itself and is the only path routes use to reach case data. An automated structural check plus code review detects bypasses (section 3).
4. **What happens on failures?** A missing membership gives `403`, or `404` for a read. A disabled actor gets `403`. An unknown role is denied. An invalid token gives `401`. An unknown case gives the generic `404`. A database outage gives a generic `500` and no protected operation (section 7).
5. **Why does check-then-write race, and what can be done?** The decision and the write are separated in time, and statements read snapshots. ADR-0005 accepts a minimum contract (section 8):
   - **Reads and lists** use one scoped statement that contains the actor and membership predicates.
   - **Writes** use a short READ COMMITTED transaction with **separate** `FOR SHARE` locking statements, first the actor and then the membership. The locks are held through the protected write and the commit.
   - **Ordering.** An operation that has already taken its locks may finish before a concurrent revocation commits. A revocation that commits first is seen, and the request is denied.

   Revocation of in-flight requests is **not** instantaneous. None of this is implemented, and the PostgreSQL locking behavior has **not** been exercised or tested.
6. **What does requester create-only mean?** A later GET returns `404`. A replay re-authorizes and returns identity only. Keys scoped to the actor make another member's key an independent operation. This is accepted as design, but not implemented or tested (section 9).

## 12. Resolution in the authorization ADR

[ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) (Accepted 2026-11-15) settles or constrains each of the items below:

- the credential lifecycle and the credential-to-actor mapping;
- the inactive-actor status code;
- enforcement placement, and the structural check that routes cannot bypass it;
- acceptance of requester create-only;
- the revocation strategy and lock order, with executed concurrency tests;
- decision caching;
- compatibility with the replay contract accepted in ADR-0004: actor-scoped keys and an identity-only replay;
- direct database writers, optional row-level security, and a least-privilege database role;
- decision logging and attribution.

Still open after acceptance:

- ~~the credential configuration and comparison mechanism~~: implemented locally (section 5.1);
- ~~the structural-check mechanism~~: implemented (section 7.1);
- row-level security and a least-privilege database role;
- verified audit attribution.

This document itself accepts no ADR.

## 13. Document review checklist

These are documentation-review items. A checked box means the statement was verified by inspecting this document against ADR-0004, ADR-0005, and the permission policy on 2026-11-15. Executed results are recorded in the test reports linked from sections 5.1 and 7.1.

- [x] Every component is labeled with its status: the HTTP boundary (localhost only), resolver, policy, authorized-case-operations layer, and structural check are implemented (sections 5.1, 7.1, and 7.2); request logging is partial. HTTP test statuses cite executed results.
- [x] Every allow path starts from a verified credential and requires an active actor, an active membership, and the action-specific permission.
- [x] Create and list use an untrusted selector; get by ID uses the persisted owner. A client-supplied organization ID can only narrow access.
- [x] The outcomes `401`, `403`, and the identical generic `404` match the [policy](authorization-permissions.md#5-suggested-http-outcomes). An inactive actor gets `403`.
- [x] Requester create-only appears as accepted in ADR-0005 and implemented in the Python layer, and neither a later read nor (by design) a replay grants read access.
- [x] The check/use options are compared, the lock semantics are cited, and only the accepted minimum revocation contract is stated. Write-side ordering is tested at the Python layer; instantaneous revocation is not claimed.
- [x] ADR-0004 and ADR-0005 are cited only as **accepted architectural decisions**. The executed results cited are the identity, credential, case authorization, and HTTP case API test reports; no keyed-creation result is claimed.

**References:** [OWASP API Security Top 10 2023 — API1: Broken Object Level Authorization](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/), [PostgreSQL 16 explicit locking](https://www.postgresql.org/docs/16/explicit-locking.html), and [PostgreSQL 16 transaction isolation](https://www.postgresql.org/docs/16/transaction-iso.html).
