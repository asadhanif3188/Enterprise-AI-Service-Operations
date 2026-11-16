# OLTP baseline: pre-optimization query plans for case read and list

**Status:** Recorded 2026-11-16. Baseline only. No index, setting or SQL was
changed for this experiment.

**Role:** This is the **baseline environment and dataset** for later index or
query work. A future before/after comparison must reuse this environment
definition, fixture, statements and method, or record exactly what differs.
Numbers measured elsewhere are not comparable with these.

This directory records how PostgreSQL executes the application's two
authorized read statements. It covers the schema exactly as migrations
`db/001`–`db/004` define it, on a fixed synthetic dataset. It answers *what
the planner does today*. It does not choose or evaluate indexes, and it is not
evidence of tenant isolation, which is tested separately (see
[Limitations](#limitations)).

| File | Content |
|---|---|
| [`run_baseline.sh`](run_baseline.sh) | One-command reproduction: disposable container, migrations, fixture, statistics, environment, plans, cleanup |
| [`sql/fixture.sql`](sql/fixture.sql) | Deterministic synthetic data (no randomness) |
| [`sql/query_a_get_case.sql`](sql/query_a_get_case.sql) | **Query A**: the authorized case read of `get_case()` |
| [`sql/query_a0_actor_status.sql`](sql/query_a0_actor_status.sql) | Context only: the actor-status check that precedes Query A |
| [`sql/query_b_list_cases.sql`](sql/query_b_list_cases.sql) | **Query B**: the authorized, organization-scoped list of `list_cases()` |
| [`sql/environment.sql`](sql/environment.sql) | Version, settings, effective indexes, exact row counts, statistics, parameter precheck |
| [`check_sql_matches_source.py`](check_sql_matches_source.py) | Verifies that the measured statements are the application's statements |
| [`capture_app_path.py`](capture_app_path.py) | Supplementary cross-check: the real methods, with plans logged by `auto_explain` |
| [`raw/`](raw/) | Unedited output of the recorded run |
| [`interpretation.md`](interpretation.md) | Reading of the plans, kept separate from the raw output |

## Measured statements

Both statements are taken verbatim from
[`src/authorized_case_operations.py`](../../src/authorized_case_operations.py).
They are not simplified, and the authorization predicates are not removed.
psycopg's named placeholders become positional ones in order of first
appearance. `check_sql_matches_source.py` parses the source and confirms that
all three statements match, ignoring whitespace only:

```text
MATCH  get_case() execute #1  ->  sql/query_a0_actor_status.sql
MATCH  get_case() execute #2  ->  sql/query_a_get_case.sql
MATCH  list_cases() execute #1  ->  sql/query_b_list_cases.sql
```

- **Query A** (`get_case()`, main statement): the case by `id`, inner-joined to
  an active membership of the caller in the case's own organization, with a
  role that grants `case:read`, and to the caller's active actor row.
- **Query A0** (context): `get_case()` first runs `SELECT is_active FROM actor
  WHERE id = $1` on the same connection. It belongs to the real read path, but
  it is not the authorized case read, and its plan is not a substitute for
  Query A.
- **Query B** (`list_cases()`): the single statement that combines an actor
  status scalar subquery, a `CROSS JOIN LATERAL` authorization `EXISTS`, and a
  `LEFT JOIN LATERAL` page of at most `$4` case rows, gated by that decision.
  The effective requested limit is 20 (`DEFAULT_LIST_LIMIT`), so `$4 = 21`:
  the extra row computes `has_more`.

The application sends each statement once, on a new autocommit connection,
with bound parameters, so PostgreSQL builds a custom plan for the actual
values. The scripts use `PREPARE` and a single `EXPLAIN ... EXECUTE` per
session, which also produces a custom plan. The supplementary `auto_explain`
capture of the real methods shows the same plan shapes (see
[interpretation](interpretation.md#application-path-cross-check)).

### Parameters (synthetic)

| Parameter | Value | Meaning |
|---|---|---|
| actor | `1a1e8387-9f2a-572f-3ade-0f8211c630f8` = `md5('eaiso-day14-actor-1')` | Active provider `service_engineer`, active membership in all three organizations |
| case (A) | `7dd97bf9-c05f-8ef8-ab05-3809f2576dc7` = `md5('eaiso-day14-case-25000')` | Exists, owned by `baseline-large` |
| roles | `{service_engineer,service_manager}` | `roles_permitting('case:read')` and `('case:list')`, sorted |
| organization (B) | `8af08632-4147-4fce-49b8-0f34277f37f1` = `md5('eaiso-day14-org-1')` | `baseline-large` (40,000 cases) |
| organization (B, supplementary) | `ba163064-53d2-177c-e4e6-611ea72ea20e` = `md5('eaiso-day14-org-3')` | `baseline-small` (1,000 cases) |
| fetch (B) | `21` | Limit 20 + 1; psycopg 3.3.9 binds it as `int2` |

`raw/environment.txt` ends with a precheck that confirms each parameter row
exists with the stated status (all four checks `t`).

## Environment (recorded run)

Execution: 2026-11-16, 11:33:46Z to 11:34:27Z (UTC). The full values are in
[`raw/environment.txt`](raw/environment.txt).

| Item | Value |
|---|---|
| PostgreSQL | **16.14** (Debian 16.14-1.pgdg13+1), x86_64, `server_version_num` 160014 |
| Image | `postgres:16`, `sha256:33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20` |
| Database / schema | `eaiso_baseline` / `public`, new and empty before the migrations |
| Connection role | `postgres` (bootstrap superuser of the disposable container), Unix socket, with the application's `lock_timeout=5000`, `statement_timeout=10000` |
| Extensions | `plpgsql` only. `gen_random_uuid()` is built in. |
| Container limits | `--cpus 2` (cgroup `cpu.max 200000 100000`), `--memory 2g` (cgroup `memory.max 2147483648`), `/dev/shm` 64 MiB. `nproc` inside reports 12 (host CPUs visible; the quota limits use). |
| Host | Docker Desktop 29.8.1 on Windows, WSL2 kernel 6.18.58.1, 12 CPUs, ~9.7 GiB assigned to the VM |
| Storage | PGDATA on the image's anonymous Docker volume (WSL2 VM disk) |
| Settings | Image defaults. `shared_buffers` 128 MB, `work_mem` 4 MB, `effective_cache_size` 4 GB, `random_page_cost` 4, `jit` on (threshold 100000), `max_parallel_workers_per_gather` 2, all `enable_*` planner switches at their defaults. `EXPLAIN (SETTINGS)` printed no `Settings:` line, meaning no planner-relevant setting differs from its default. |
| Planner overrides | None (no `enable_seqscan = off` or similar) |
| Autovacuum | On, default. No autovacuum or autoanalyze ran during the captures (`raw/final-row-counts.txt`). |

### Effective indexes (all constraint-backed; none added for this experiment)

"Pre-index" here means *before optimization indexes*, not *no indexes*.

| Table | Index | Columns | Origin |
|---|---|---|---|
| `case_record` | `case_record_pkey` | `(id)` | Primary key (`db/001`) |
| `case_record` | `case_record_organization_case_key` | `(organization_id, id)` | `UNIQUE` added by `db/004` for the idempotency composite foreign key. **The planner can, and does, use it for organization filters.** |
| `organization_membership` | `organization_membership_pkey` | `(actor_id, organization_id)` | Primary key (`db/003`) |
| `actor` | `actor_pkey` | `(id)` | Primary key |
| `organization` | `organization_pkey`, `organization_slug_key` | `(id)`, `(slug)` | Primary key, `UNIQUE` |
| `case_status_event` | `case_status_event_pkey`, `case_event_one_per_revision` | `(id)`, `(case_id, to_version)` | Primary key, `UNIQUE` |
| `ai_suggestion` | `ai_suggestion_pkey` | `(id)` | Primary key |
| `case_creation_idempotency` | `case_creation_idempotency_pkey`, `case_creation_idempotency_case_unique` | `(organization_id, actor_id, operation, idempotency_key)`, `(case_id)` | Primary key, `UNIQUE` |

There is no index on `case_record(created_at)`, on
`case_record(organization_id, created_at, ...)`, or on
`organization_membership(organization_id)`.

## Dataset

Generated by [`sql/fixture.sql`](sql/fixture.sql) with fixed formulas.
Identifiers are `md5('eaiso-day14-<kind>-<n>')::uuid`, and timestamps are
fixed, so every run produces identical data. All names and text are
fictional. There are no credentials and no real customers.

**Size choice.** 50,000 cases across three organizations of deliberately
different sizes (80% / 18% / 2%). That is enough to make `case_record` about
22 MB (2,766 heap pages), over the 8 MB parallel-scan threshold, so
organization selectivity decides the access path. It is small enough to build
in seconds and to cache entirely in the 128 MB `shared_buffers`. It is modest
by design. It is not a production volume, and Week 3 is expected to use at
least two documented sizes.

Actual counts, measured after loading (`raw/environment.txt`) and again after
all captures (`raw/final-row-counts.txt`, unchanged):

| Table | Rows | Distribution |
|---|---:|---|
| `organization` | 3 | `baseline-large`, `baseline-medium`, `baseline-small` |
| `case_record` | 50,000 | 40,000 / 9,000 / 1,000. Assigned by `n % 50` (0–39 large, 40–48 medium, 49 small), so the organizations are **interleaved** in creation and physical order. `created_at = 2026-01-01 00:00 UTC + n × 10 min`, unique, inserted in ascending order (correlation 0.99999946). All `NEW`, version 1. |
| `actor` | 37 | 6 active provider actors (3 engineers, 3 managers), 1 inactive provider engineer, 30 customer requesters (10 per organization) |
| `organization_membership` | 49 | Providers 1–6 in all three organizations (18, one inactive), the inactive actor in `baseline-large` (1), each requester in its home organization (30). Eligibility violations: 0. |
| `case_status_event`, `ai_suggestion`, `case_creation_idempotency` | 0 | Not used by the measured statements |

Memberships are inserted through the `db/003` eligibility trigger, as trusted
provisioning would insert them. After loading, `VACUUM (ANALYZE)` runs once,
database-wide. That gives the planner statistics and sets hint bits and the
visibility map, so the first measured read does not pay that one-time cost.

## Method

1. Start a **new** `postgres:16` container with the limits above and a
   per-run password that is never written anywhere. No other database or
   container is touched.
2. Apply `db/001`–`db/004` with `ON_ERROR_STOP=1`, load the fixture, run
   `VACUUM (ANALYZE)`, and record the environment.
3. For each statement: restart PostgreSQL, then run the script **six times,
   each in a new psql session**, because the application opens a new
   connection per call. Each run executes
   `EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT TEXT)` once.
   - **Run 1** follows the restart, so `shared_buffers` is empty. The WSL2
     VM's page cache is **not** cleared, so run 1 is "PostgreSQL-cold, OS
     cache uncontrolled", not a true cold read.
   - **Runs 2–6** follow immediately, with warm `shared_buffers`.
4. Supplementary: run Query B again for `baseline-small`. Then enable
   `auto_explain` for this database only, call the real `get_case()` and
   `list_cases(limit=20)` once through `capture_app_path.py`, and reset the
   setting.
5. Re-count the rows, then remove the container and its volume.

Every observation is kept individually in `raw/`. No percentiles or averages
are reported; six runs per statement on a desktop VM do not support them.
Execution time is the server-side executor time of **one statement**. It
excludes connection setup, the network, the other statement in `get_case()`,
credential resolution, Python, HTTP and serialization. **It is not API
latency.**

The recorded output comes from the third invocation of the script:

- The first stopped at the supplementary step because of a Git Bash path
  translation error, which is fixed in the script.
- The second completed, but its index listing also printed foreign keys that
  reference an index. That query was corrected.
- Neither change touches the fixture, the statements or the measurement steps.
- The earlier plans that were inspected had the same shapes and buffer counts
  as the recorded run: Query A run 1, a warm Query B run for the large
  organization, a warm small-organization run, and Query A0 run 1. Not every
  earlier run was inspected.
- Only the third invocation's output is kept.

**Script revision after the recorded run.** A code review found robustness
issues in `run_baseline.sh`, which were fixed before commit:

- The existing-container refusal could be skipped under `pipefail`.
- Cleanup removed the container by name. It now removes only the container
  ID it created.
- Setup now runs without the application timeouts.
- The `auto_explain` log window now uses the container clock, and the script
  asserts that the log holds exactly 3 plans.

The fixture, statements, settings and capture loop are unchanged. The
committed script was then run once more into a separate directory, and that
output was not kept. It reproduced the same row counts, plan shapes and
buffer counts (Query A 5; Query B 2,777 warm / 2,768 read cold; small
organization 1,022 / 1,013). Only timings differed: large-organization warm
runs took 15.7–19.7 ms against 23.4–45.9 ms in the recorded run.

## Reproduce

Prerequisites: Docker, bash (Git Bash on Windows), and the `postgres:16` image.
For the optional cross-check, a Python environment with
`requirements-api.txt` installed. From the repository root:

```bash
python experiments/oltp-baseline/check_sql_matches_source.py
BASELINE_PYTHON=.venv/Scripts/python.exe experiments/oltp-baseline/run_baseline.sh   # omit BASELINE_PYTHON to skip the cross-check
```

The script refuses to reuse an existing container name (`BASELINE_CONTAINER`,
default `eaiso-oltp-baseline`) and removes its container on exit
(`KEEP_CONTAINER=1` keeps it). It overwrites `raw/`. To preserve this record,
set `BASELINE_OUT` to another directory. Pin the image by digest
(`BASELINE_IMAGE=postgres@sha256:33f9…5e20`) for an exact match. A newer
`postgres:16` minor version can plan differently.

## Limitations

- **Not a security test.** A plan shows how rows are fetched, not that
  another tenant's rows can never be returned. Tenant isolation and the
  authorization outcomes are covered by the existing PostgreSQL-backed suites
  (`tests/test_case_authorization.py`, `tests/test_http_case_api.py`,
  `tests/test_case_access_boundary.py`). Those suites were rerun on a separate
  disposable database for this review: 592 passed.
- **One synthetic dataset, one size, one machine.** The data has interleaved
  organizations, uniform text sizes, append-only `created_at` order and no
  updates or dead tuples. Real distributions, bloat, concurrency and larger
  membership tables can change plans.
- **Small identity tables.** `actor` (37 rows) and `organization_membership`
  (49 rows) each fit in one page, so sequential scans of them are expected
  here. The plans say nothing about these lookups at a larger scale.
- **Timing noise.** The host is a desktop Docker VM with a 2-CPU quota. Warm
  Query B runs varied from 23 to 46 ms with identical plans and buffer counts.
  Timings show order of magnitude only. Buffer counts and plan shape are the
  stable evidence.
- **Cache state.** Run 1 is PostgreSQL-cold only (see Method). True cold-disk
  behavior was not measured.
- **Sampled statistics.** `ANALYZE` samples about 30,000 of the 50,000
  `case_record` rows (default statistics target 100). Estimates can therefore
  vary slightly between rebuilds, for example 39,993 or 40,053 for the large
  organization. Plan choice was the same in every build observed. The target
  was not raised, because that is a planner-relevant change.
- **Superuser connection.** The application's least-privilege role is not
  modeled here. Privileges do not change these plans, and no row-level
  security exists.
- **Only the default list page.** One page (first page, limit 20) for two
  organizations. There is no cursor pagination yet, and deeper pages, other
  limits and the medium organization were not measured.
- **No capacity or SLO claims.** Nothing here supports extrapolated
  throughput, production latency, or improvement percentages.
