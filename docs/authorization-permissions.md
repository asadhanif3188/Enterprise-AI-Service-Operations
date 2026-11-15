# Case Authorization — Minimum Role-Permission Policy

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Status:** **Design only.** The policy is **accepted as architecture** in [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) (2026-11-15) but is **not implemented**. No HTTP endpoint, policy code, or authorization test exists, and nothing here has been executed. A local credential resolver exists ([`src/credential_auth.py`](../src/credential_auth.py)) but establishes identity only. The `actor` and `organization_membership` tables now exist ([`db/003_actor_identity_and_grants.sql`](../db/003_actor_identity_and_grants.sql)), but no request reads them.  
**Scope:** The minimum organization-scoped permissions for `POST /cases`, `GET /cases/{id}`, and `GET /cases`, as proposed in the [API contract](api-contract.md).  
**Decision boundary:** This document is the detailed policy behind [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md), which is **Accepted**. It is not an implementation and not proof of tenant isolation. ADR-0005 decides where and how the policy is enforced. A proposed enforcement sequence is sketched in [Server-side authentication and authorization sequence](authentication-authorization-sequence.md).

## 1. Baseline

- **What exists:** `organization` and `case_record` are implemented ([`db/001_init.sql`](../db/001_init.sql)), and so is `case_status_event` ([`db/002_case_status_events.sql`](../db/002_case_status_events.sql)).
- **What exists for identity:** `actor` and `organization_membership`, with the ADR-0006 role-grant eligibility guards and trusted local provisioning ([identity model](identity-model.md), [test results](../tests/identity-grant-integrity-results.md)). They hold data only; this policy is not enforced anywhere.
- **Current case service:** [`src/case_service.py`](../src/case_service.py) filters queries by a caller-supplied organization ID. That is query scoping, not authorization.
- **Governing decisions:**
  - [ADR-0001](adr/0001-postgresql-authoritative-case-store.md) and [ADR-0003](adr/0003-application-controlled-case-transitions.md) require application-controlled access but do not implement it.
  - [ADR-0002](adr/0002-ai-suggestions-non-authoritative.md) means AI output never authorizes anything.
  - [ADR-0004](adr/0004-keyed-idempotent-case-creation.md) (Accepted design, not implemented) scopes idempotency keys to the *authorized* organization and the *verified* actor.

This document defines **which** decisions the policy must make. It deliberately does not repeat the entity design or the endpoint contract.

## 2. Permission vocabulary

The initial vocabulary has exactly three permissions. Any other action is outside it, and is therefore denied.

| Permission | Target | Organization the decision is evaluated against | API operation |
|---|---|---|---|
| `case:create` | One organization, nominated by the client | The request body's `organization_id`, used as a selector only | `POST /cases` |
| `case:read` | One existing case | The case's **persisted** `case_record.organization_id`; the client never supplies it | `GET /cases/{id}` |
| `case:list` | One organization, nominated by the client | The required `organization_id` query parameter, used as a selector only | `GET /cases?organization_id=…` |

For service engineers and managers, `case:read` and `case:list` cover **every case in a member organization**. This is a deliberate minimal-demonstration choice. It is not assignment-based access and not least-privilege access to individual cases.

## 3. Role-permission matrix

**Allow** applies only when every predicate in section 4 holds. A role name on its own never grants access. A blank cell is impossible: every combination is either Allow or Deny.

| Actor and membership state for the evaluated organization | `case:create` | `case:read` | `case:list` |
|---|---|---|---|
| Active actor, active `service_engineer` membership | **Allow** | **Allow** | **Allow** |
| Active actor, active `service_manager` membership | **Allow** | **Allow** | **Allow** |
| Active actor, active `requester` membership | **Allow** (submission only) | **Deny** | **Deny** |
| Active actor, no membership in that organization | Deny | Deny | Deny |
| Active actor, **inactive** membership | Deny | Deny | Deny |
| **Inactive** actor, with any membership | Deny | Deny | Deny |
| Membership role not in the mapping below | Deny | Deny | Deny |
| `platform_administrator` responsibility with no membership in that organization | Deny | Deny | Deny |
| Missing, invalid, unrecognized, or revoked credential | Deny | Deny | Deny |

Role-to-permission mapping (proposal, not code):

```text
service_engineer -> {case:create, case:read, case:list}
service_manager  -> {case:create, case:read, case:list}
requester        -> {case:create}
any other value  -> {}            # unknown roles get no permissions; there is no default allow
```

No role inherits permissions from another. A membership in one organization grants nothing in another. No superuser exception exists. `platform_administrator` is a responsibility in the [requirements](requirements.md#3-users-and-responsibilities), not a membership role ([identity model §4](identity-model.md#4-organization_membership)). It confers **no** cross-customer access.

### Requester: create-only (accepted in ADR-0005)

Earlier guidance listed requester creation as a *candidate*. This document proposed settling it as follows, and [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) **accepted** the rule on 2026-11-15:

- **Create:** an active requester may `case:create` only in an organization where it holds an active `requester` membership.
- **Read and list:** the requester receives **no** `case:read` and **no** `case:list`, not even for cases it submitted.

The rule is accepted as architecture. It is **not implemented**.

**Rationale.** The requirements name case submission as the requester's responsibility ([§3](requirements.md#3-users-and-responsibilities), [§4 step 1](requirements.md#4-core-workflow)). Granting create therefore serves an intended business function. Reading is withheld because `case_record` has no requester-ownership relation, so an organization-wide read would expose other requesters' cases.

**Consequences:**

- The requirement that a requester can "view its permitted status" is **deferred**, not contradicted, until per-case requester ownership is modeled.
- The `201 Created` response returns the case just created as part of the create operation. That is not a `case:read` grant: a later `GET` of the `Location` URL returns `404` to the requester.

## 4. Policy predicates

The policy is **pseudocode for review**, not an implementation:

```text
decide(credential, action, request):
  actor_id := resolve_from_server_controlled_config(credential)
  if actor_id is none:                                   -> 401
  if not actor(actor_id).is_active:                      -> 403
  org := persisted case_record.organization_id           if action = case:read
         request.organization_id (selector only)         if action in {case:create, case:list}
  if action = case:read and the case does not exist:     -> deny   (404)
  m := organization_membership(actor_id, org)
  if m is none or not m.is_active:                       -> deny
  if action not in PERMISSIONS[m.role]:                  -> deny   (unknown role => empty set)
  allow, and run the case operation restricted to organization_id = org
```

An allow requires **all** of the following:

1. **A server-verified actor ID only.** `actor_id` comes from verifying a credential against server-controlled, test-only configuration. It never comes from headers such as `X-User-Id`, `X-Actor-Id`, or `X-Org-Id`, from a display name, from decoded but unverified token claims, from the request body, or from a request or correlation ID.
2. **An active actor.**
3. **An active membership** for exactly `(actor_id, org)`.
4. **An action-specific permission** for that membership's role, computed separately for each organization.
5. **The persisted case owner for single-case access.** For `case:read`, `org` is read from the stored case, and the case query stays restricted to that organization.
6. **The request organization as a selector, never a credential.** A client-supplied `organization_id` only chooses which membership is evaluated. If that membership is absent, inactive, or lacks the permission, the request is denied. Under this policy, a supplied organization ID can never widen access beyond the verified actor's own memberships.

Any missing predicate, lookup failure, or unexpected value results in **deny**. The check/use race and revocation timing are open (section 8).

## 5. Suggested HTTP outcomes

These follow the [API contract error table](api-contract.md#8-proposed-error-contract).

| Condition | Outcome | Disclosure rule |
|---|---|---|
| Missing, invalid, unrecognized, or revoked credential | `401 Unauthorized` | No fallback to any identity header |
| Verified actor, explicit target organization not authorized for `case:create` or `case:list` (no membership, inactive membership, or role lacks the permission) | `403 Forbidden` | No case rows; no membership details |
| `GET /cases/{id}`: case does not exist, **or** exists in an organization the actor is not authorized to read | The **same** generic `404 Not Found` | The two cases must be indistinguishable in status and body |
| Requester calls `GET /cases/{id}` in its own organization | `404 Not Found` (falls under the previous row) | Same as above |
| `GET /cases` without `organization_id`, or with an invalid one | Rejected as invalid; the contract's `400`/`422` split is not settled for this case | Never falls back to listing all organizations |
| Verified credential, **inactive actor** | `403 Forbidden`, always denied (ADR-0005). A revoked credential gets `401` instead. | No case data |
| Attempted HTTP update or transition | No such route exists, and nothing is mutated | Not evidence that authorized updates are enforced |

Error bodies never include foreign case data, SQL, credentials, or membership lists.

## 6. Proposed test vectors

Every vector below is **proposed, not executed**. No policy, API, or security test has run. When implemented, each vector must assert both the HTTP response **and** the database state in the dedicated test database. Organizations A and B and all actors are fictional.

| ID | Verified actor and memberships | Request | Expected outcome | State assertion | Status |
|---|---|---|---|---|---|
| V01 | Engineer, active in A | `POST /cases` for A | Allow, `201` | Exactly one new case, owned by A | Proposed, not executed |
| V02 | Engineer, active in A | `GET` a case owned by A | Allow, `200` | No change | Proposed, not executed |
| V03 | Engineer, active in A | `GET /cases?organization_id=A` | Allow, `200` | Only A's cases returned | Proposed, not executed |
| V04 | Manager, active in A | `POST` for A; list A | Allow, `201`; allow, `200` | One new case owned by A; list returns A's cases only | Proposed, not executed |
| V05 | Requester, active in A | `POST /cases` for A | Allow, `201` | One new case owned by A | Proposed, not executed |
| V06 | Requester, active in A | `GET` the case it just created, then another A case | `404` for both | No change | Proposed, not executed |
| V07 | Requester, active in A | List A | Deny, `403` | No rows in the response | Proposed, not executed |
| V08 | Engineer, active in A only | `GET` a case owned by B | Deny, `404` | Response identical to V09 | Proposed, not executed |
| V09 | Engineer, active in A | `GET` a nonexistent UUID | `404` | Response identical to V08 | Proposed, not executed |
| V10 | Engineer, active in A only | List B | Deny, `403` | No rows in the response | Proposed, not executed |
| V11 | Engineer, active in A only | `POST /cases` for B | Deny, `403` | **No** `case_record` row inserted; B's case count unchanged | Proposed, not executed |
| V12 | Mixed roles: engineer in A, requester in B | List A; list B; `POST` for B; `GET` a case owned by B | `200`; `403`; `201`; `404` | Roles evaluated per organization; one new B case; no B rows returned | Proposed, not executed |
| V13 | Engineer, **inactive** membership in A | `GET` a case owned by A; list A | `404`; `403` | No change | Proposed, not executed |
| V14 | **Inactive** actor, active engineer membership in A | `POST /cases` for A | Deny, `403` | No row inserted | Proposed, not executed |
| V15 | Active actor, no membership in A | List A | Deny, `403` | No rows in the response | Proposed, not executed |
| V16 | Policy evaluated with a role value outside the mapping (policy-level test) | `case:create` | Deny | No row inserted | Proposed, not executed |
| V17 | Platform-administrator responsibility, no membership in A | List A; `GET` a case owned by A | `403`; `404` | No rows; no case data | Proposed, not executed |
| V18 | No credential | `POST /cases` for A | `401` | No row inserted | Proposed, not executed |
| V19 | Invalid credential, plus an `X-Actor-Id` header naming an engineer in A | List A | `401` | Header ignored; no rows | Proposed, not executed |
| V20 | Valid engineer-in-A credential, plus spoofed `X-Actor-Id` and `X-Org-Id` headers pointing at B | `GET` a case owned by B; list A | `404`; `200` with A's cases only | The resolved principal and its memberships are unchanged by headers | Proposed, not executed |
| V21 | Engineer, active in A | `GET /cases` without `organization_id` | Rejected as invalid | No rows from any organization | Proposed, not executed |
| V22 | Engineer, active in A | Any HTTP status change for an A case | No route | `status` and `version` unchanged; not evidence of transition authorization | Proposed, not executed |

## 7. Explicitly excluded permissions

This policy grants none of the following. They need separate designs, and in several cases ADRs:

- **Case content update** of any kind. No update route exists.
- **Case status transition over HTTP.** `transition_case()` stays local and unauthenticated ([ADR-0003](adr/0003-application-controlled-case-transitions.md)).
- **AI suggestion review, approval, or execution** (FR-06, FR-07), and requesting AI triage through the API ([ADR-0002](adr/0002-ai-suggestions-non-authoritative.md)).
- **Membership or actor management**: granting, revoking, or changing roles.
- **Global or platform-administrator access** to customer data, support access, or emergency elevation.
- **Case assignment**, and any access derived from assignment.

## 8. Open issues

None of these is resolved by this document.

- **Revocation timing and the check/use race.** A membership check made separately from the case operation can race with a revocation. Even a single statement uses a snapshot. ADR-0005 accepts a **minimum** contract, not instant revocation: scoped single statements for reads and lists, and short write transactions holding `FOR SHARE` locks on the actor, then the membership. Its behavior under concurrency is untested.
- **Role-grant eligibility.** This policy defines what a granted role may do. Who may *receive* each role, by actor affiliation, is decided separately in [ADR-0006](adr/0006-actor-affiliation-and-role-grant-eligibility.md) (Accepted; enforced when grants are written by database guards and provisioning). It adds no request-time predicate and does not change this matrix.
- **Requester case ownership.** There is no creator or requester relation on `case_record`. Requester follow-up access ("view its permitted status") needs its own design.
- **Idempotent replay for requesters.** [ADR-0004](adr/0004-keyed-idempotent-case-creation.md) addresses this. It is **Accepted** as design, but nothing is implemented, and requester create-only itself is still only proposed:
  - every retry is authorized for `case:create` **before** any key lookup;
  - keys are scoped to `(authorized organization_id, verified actor_id, 'case:create', key)`, so another member's key is an independent operation;
  - a replay returns only `id` and `organization_id`, never the case's current state.

  `case:create` is **not** `case:read`. A replay must never act as a read of the current state.
- **Audit attribution.** `case_status_event.actor_label` stays unverified. Denied attempts and successful creates are not attributed to a verified actor anywhere.
- **Breadth of engineer and manager visibility.** Organization-wide visibility should be revisited once assignment, sensitive case classes, or internal teams exist.
- **Optional PostgreSQL row-level security** as defense in depth.
- **A least-privilege database role**: the local components still use the development account.
- **Test-only credentials**: how they are configured and resolved without committing secrets.
- **Enforcement details beyond ADR-0005.** [ADR-0005](adr/0005-server-verified-identity-and-tenant-authorization.md) (Accepted) settles where enforcement lives, the inactive-actor status (`403`), and requester create-only. Still open: the structural no-bypass check mechanism, the credential configuration mechanism, and whether row-level security is added for defense in depth.

## 9. Design review checklist

These are documentation-review items. A checked box means the statement was verified by inspecting this document against ADR-0005 and the linked documents on 2026-11-15. It is **not** evidence that authorization is implemented or tested.

- [x] Every permission in sections 2 and 3 uses only `case:create`, `case:read`, or `case:list`, with the role names `service_engineer`, `service_manager`, and `requester`.
- [x] Every allow path requires all six predicates in section 4. No path allows access based on a role name, header, display name, or supplied organization ID alone.
- [x] Unknown roles, inactive actors, inactive memberships, and missing memberships are always denied.
- [x] `case:read` evaluates the persisted case owner; a nonexistent case and a foreign case produce the same `404`.
- [x] Requester create-only is presented as accepted in ADR-0005, but not implemented, with its deferred status-viewing consequence stated.
- [x] `platform_administrator` has no implicit cross-customer access.
- [x] All test vectors are marked proposed and not executed, and each states a database-state assertion.
- [x] Excluded permissions and open issues are recorded, not silently decided.
- [x] Consistent with the [identity model](identity-model.md), the [API contract](api-contract.md), the [requirements](requirements.md), the [data model](data-model.md), and the [ADR register](adr/README.md#register).
