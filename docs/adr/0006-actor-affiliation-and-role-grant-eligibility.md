# ADR-0006: Actor Affiliation and Role-Grant Eligibility

- **Status:** Proposed, for architecture-owner review. **Not accepted, and not implemented.**
- **Recorded:** 2026-11-15
- **Decision provenance:** Contemporary proposal. It follows the provider/customer distinction documented on 2026-11-15 in [Actor and Organization Membership, §10](../identity-model.md#10-provider-affiliation-vs-customer-access-grants). No implementation, test, or measured comparison supports it yet.
- **Related requirements:** FR-01 and FR-02; rules "Organization ownership" and "Auditability"; quality requirement "Security" ([requirements](../requirements.md))
- **Related decisions:**
  - [ADR-0005](0005-server-verified-identity-and-tenant-authorization.md) (Accepted): permissions and request-time enforcement.
  - [ADR-0004](0004-keyed-idempotent-case-creation.md) (Accepted): actor- and organization-scoped idempotency.
  - This ADR supersedes neither.
- **Implementation evidence:** None.
  - No `actor` table, `organization_membership` table, affiliation columns, provisioning path, eligibility trigger, or eligibility test exists.
  - [`db/001_init.sql`](../../db/001_init.sql) defines only the customer `organization` table, `case_record`, and `ai_suggestion`.

## Problem

The fictional ABC service provider employs engineers and managers who serve customer Organizations A and B; customer employees submit cases. Under ADR-0005, an active membership's role determines which case permissions are available.

Without a separate **role-grant eligibility** invariant, a mistake becomes a privilege escalation. For example, assigning a customer employee the `service_manager` role in their own organization would give that employee read and list privileges.

The proposed `organization_membership` relation represents **access to a customer**, not employment by that customer. A person's affiliation must be distinguished from the roles that may be provisioned for them, without changing ADR-0005's request-time permission matrix.

## Constraints

- **Customer tenants only.** `organization` contains customer tenants only. ABC is the implicit single provider, not an `organization` row, and each case still has exactly one customer owner.
- **ADR-0005 is Accepted.** A verified actor, an active membership, a permission, and an organization scope govern case requests.
  - Affiliation alone **never grants case access**.
  - The role vocabulary remains `service_engineer`, `service_manager`, and `requester`; `platform_administrator` is not a membership role.
- **Scope of this model.** An ABC employee may need **separate grants for several customers**. A customer employee initially belongs to one home customer. Contractors, identities with several employers, and service principals are outside this first model.
- **Nothing exists yet.** There is no trusted provisioning API, credential resolver, application database role, affiliation schema, or role-grant enforcement. The local demonstration must not be presented as production-grade identity and access management.

## Options Considered

1. **Provider/customer fields on `actor`, with eligibility checked by controlled provisioning and by a PostgreSQL invariant (preferred).** Add `affiliation_type` and `home_organization_id`, and check allowed active grants on membership writes and on affiliation changes.
   - *Benefits:* an explicit, testable separation; a small schema; defense against accidental invalid direct writes to membership.
   - *Costs:* cross-table checks, lock ordering, trigger maintenance, and limited support for multiple affiliations.
2. **Keep affiliation in synthetic-fixture conventions, and enforce eligibility in provisioning code alone.**
   - *Benefits:* the lowest implementation cost.
   - *Costs:* actors cannot be told apart in stored data, and another write path can create an invalid active grant. There is no database-level eligibility guarantee.
3. **Dedicated affiliation or party tables, with a richer role-assignment model.**
   - *Benefits:* can model contractors and multiple employers.
   - *Costs:* premature schema, lifecycle, and policy complexity for two synthetic customers and one provider.

## Decision

**Proposed: adopt option 1 for the initial synthetic demonstration**, subject to architecture-owner approval before the identity migration.

1. **Persist the business affiliation.** Extend the proposed `actor` entity with two server-managed attributes:
   - `affiliation_type`: `NOT NULL`, with `CHECK (affiliation_type IN ('provider', 'customer'))`;
   - `home_organization_id`: nullable, `REFERENCES organization(id) ON DELETE RESTRICT`.

   A same-row `CHECK` enforces exactly one valid shape:
   - `provider` requires `home_organization_id IS NULL`. ABC is implicit; there is no provider tenant row.
   - `customer` requires `home_organization_id IS NOT NULL`, naming one specific customer.

   The existing proposed `actor.id`, `is_active`, and `organization_membership(actor_id, organization_id, role, is_active)` remain. Keep the `organization_membership` table name for compatibility, and define it explicitly as a **customer access grant**, not employment.
2. **Allowed active grants. Every other combination is denied:**

   | Actor affiliation | Target customer | Grant role | Eligibility |
   |---|---|---|---|
   | `provider` | Any explicitly selected customer | `service_engineer` | Allow |
   | `provider` | Any explicitly selected customer | `service_manager` | Allow |
   | `provider` | Any customer | `requester` | Deny in the initial model |
   | `customer` | Exactly `home_organization_id` | `requester` | Allow |
   | `customer` | Any other customer | `requester` | Deny |
   | `customer` | Any customer | `service_engineer` or `service_manager` | Deny |

   - *Allow* means **eligible for an explicitly approved grant**, not granted automatically.
   - A provider engineer creates cases for a customer through the `case:create` permission of a `service_engineer` grant; no requester grant is needed.
   - Provider actors with administrative responsibilities receive **no automatic customer grants**.
   - Inactive grants are non-authoritative historical records, and reactivating one must revalidate eligibility.
3. **Protect grant provisioning.**
   - **Trusted path only.** Initial grants and affiliation values are created through a trusted, controlled local provisioning path, using synthetic data. No untrusted caller may set affiliation or create grants.
   - **Validation.** Every grant **insert, role or organization change, or reactivation** must validate eligibility.
   - **No API.** There is no membership-management HTTP API in this increment. A future administrative API needs its own authorization decision.
   - **Evidence.** The provisioning path must be auditable in the implementation evidence.
4. **Enforce the cross-table invariant in PostgreSQL as well as in provisioning logic.**
   - **Same-row check.** A same-row `CHECK` enforces the actor's affiliation shape.
   - **Membership-side guard.** For example, a trigger checks the actor's stored affiliation and home customer before allowing an **active** grant.
   - **Actor-side guard.** It checks that a change to the affiliation or home customer does not leave any incompatible active grant.
   - **No cross-table `CHECK`.** Do **not** use a `CHECK` constraint that queries another table. PostgreSQL does not support that as a durable cross-table invariant: it re-evaluates a `CHECK` only when its own row changes.
   - **Why triggers.** A composite foreign key that copies `affiliation_type` into the grant could enforce the provider/customer role split declaratively. It cannot easily enforce equality with the home customer, so the trigger approach is preferred, subject to tests.
   - **Privileged SQL.** Do not assume that application validation alone stops privileged SQL from bypassing the rule.
5. **Concurrent mutation contract.**
   - **Actor first.** Every ordinary provisioning transaction must lock the **actor row first** before writing its grants, and must touch multiple memberships in a deterministic order.
   - **Membership guard locks the actor.** The membership-side guard must lock the actor row explicitly, for example `FOR SHARE`, so that a concurrent affiliation update cannot commit in between. The automatic `FOR KEY SHARE` lock taken by the `actor_id` foreign key does **not** block updates to non-key columns such as `affiliation_type`, so it does not serialize these changes. This is the same distinction ADR-0005 makes.
   - **Why the actor-side guard depends on that lock.** Under READ COMMITTED, the actor-side guard sees only *committed* grants, so it relies on the membership guard's actor lock to make concurrent changes visible.
   - **Lock order inside a trigger.** A trigger fired by a membership `UPDATE` acquires its actor lock *after* the membership row lock. That reverses the actor-first order, so provisioning must take the actor lock explicitly first. The trigger's own lock only protects paths that do not follow the protocol, and such paths can deadlock; PostgreSQL detects a deadlock and aborts one transaction.
   - **Compatibility with ADR-0005.** ADR-0005's request-time transactions lock the actor, then the membership, `FOR SHARE`. Provisioning follows the same actor-first order.
   - **Affiliation changes.** An affiliation change is **rejected**, not cascaded, while incompatible active grants exist. Those grants must first be deactivated or reconciled in a controlled transaction. Inactive historical grants may remain but can never be reactivated without validation.
   - **Not proven.** The trigger code and PostgreSQL lock behavior must be demonstrated in concurrent integration tests. This ADR makes **no untested claim of race freedom**.
6. **Preserve ADR-0005 at request time.**
   - The authorized-case-operations layer still verifies the actor, the active membership, the requested action, and the case-owning organization under ADR-0005.
   - Affiliation is **not an independent grant** and does not widen request-time permissions.
   - This ADR governs which membership rows may become active. It does not add a policy branch to each HTTP handler or a new request-time predicate.
   - ADR-0004 continues to use the verified `actor.id` and the authorized customer in its idempotency scope.

## Risks and Consequences

- **Affiliation provenance.** A compromised provisioner could wrongly mark a customer actor as `provider`; a schema constraint cannot prove that someone works for ABC. Write privileges for affiliation and grants must be restricted to a trusted provisioning mechanism. Application validation and triggers do not stop:
  - a superuser;
  - a table owner who disables triggers;
  - a session that sets `session_replication_role` to `replica`;
  - uncontrolled direct SQL access.
- **Concurrency and operational friction.** Cross-table validation brings locking, deadlock, migration, and restore concerns. For example, `pg_dump` restores table data before it creates triggers, so restored rows are not revalidated.
  - Test multi-session grant inserts and reactivations against concurrent affiliation changes.
  - Document timeout and deadlock handling, and recovery.
  - Release row locks promptly, and never hold them across AI or network calls.
- **Limited model.** A customer actor has one home customer, and providers cannot receive requester grants. Contractors, multiple affiliations, or service principals require an explicit redesign, not a misleading use of the current columns.
- **Offboarding and audit.** Inactive grants may be kept without granting access, but the proposed membership table stores no complete history of grant changes. Reliable attribution and governance are future work, and this must not be called an audit trail.
- **No implementation evidence.** This proposal changes no accepted ADR. Its invariant is not operative until it is implemented, exercised, and reviewed.

## Revisit When

- **Before Accepted.** The architecture owner approves:
  - the affiliation schema;
  - the eligibility matrix, including the denial of provider requester grants;
  - the controlled provisioning boundary;
  - the treatment of inactive historical grants;
  - the policy for changing an actor's affiliation;
  - the database enforcement strategy, including the trigger-internal lock order noted in decision 5.

  The owner also confirms that this decision *complements*, and does not modify, the request-time contract in ADR-0005.
- **Before identity and grant provisioning is enabled.** Implement and test:
  - same-row constraints;
  - the membership-side and actor-side guards;
  - unauthorized cross-customer grants and customer-to-engineer grants;
  - invalid reactivation;
  - provider grants for several customers;
  - denial of provider requester grants;
  - actor affiliation changes;
  - concurrent actor and grant updates under controlled locks;
  - direct writes through the intended non-owner database role.

  Record positive and negative results and the database state. A trigger definition or a documentation checklist is not passing evidence.
- **Before a customer-facing or production deployment.** Design:
  - secure proof of actor affiliation, and authorization for provisioning;
  - least-privilege database credentials;
  - a verified history of grant changes;
  - operational recovery from mistaken grants.

  Revisit for contractors, shared service principals, operation with several providers, or new kinds of role.

**Technical references:** [PostgreSQL 16 constraints](https://www.postgresql.org/docs/16/ddl-constraints.html), [CREATE TRIGGER](https://www.postgresql.org/docs/16/sql-createtrigger.html), and [row-level locking](https://www.postgresql.org/docs/16/explicit-locking.html).
