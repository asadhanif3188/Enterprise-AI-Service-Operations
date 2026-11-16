# Interpretation of the baseline plans

This is a reading of the raw output in [`raw/`](raw/). The environment,
dataset and method are in the [README](README.md). Each number below is
copied from a named raw file. Where a statement is an inference rather than
an observation, it says so.

## Query A: authorized single-case retrieval

Raw: [`raw/query-a-get-case.txt`](raw/query-a-get-case.txt). Statement:
[`sql/query_a_get_case.sql`](sql/query_a_get_case.sql).

**Plan (identical in all 6 runs):**

```text
Nested Loop
  -> Nested Loop  (Inner Unique; Join Filter: c.organization_id = m.organization_id)
       -> Index Scan using case_record_pkey on case_record c   Index Cond: c.id = $3
       -> Seq Scan on organization_membership m                Filter: is_active AND role = ANY($2) AND actor_id = $1
  -> Seq Scan on actor a                                       Filter: is_active AND id = $1
```

| Run | Cache state | Planning | Execution | Executor buffers | Planning buffers |
|---|---|---:|---:|---|---|
| 1 | After restart (PostgreSQL-cold) | 1.222 ms | 0.144 ms | shared read=5 | hit=284 read=37 |
| 2 | Warm | 0.819 ms | 0.109 ms | shared hit=5 | hit=321 |
| 3 | Warm | 1.229 ms | 0.148 ms | shared hit=5 | hit=321 |
| 4 | Warm | 0.689 ms | 0.064 ms | shared hit=5 | hit=321 |
| 5 | Warm | 0.634 ms | 0.061 ms | shared hit=5 | hit=321 |
| 6 | Warm | 0.728 ms | 0.071 ms | shared hit=5 | hit=321 |

- **Access path.**
  - The case is located by the `case_record` primary key: 3 buffers (index
    plus heap).
  - `organization_membership` (49 rows) and `actor` (37 rows) each occupy one
    page, so each is read by a one-page sequential scan.
  - The membership primary key `(actor_id, organization_id)` exists but is not
    used. That is expected at one page and says nothing about larger
    membership tables.
- **Joins.** Two nested loops, each with one outer row.
- **Authorization placement.** The membership predicates (actor, active, role)
  are a scan filter. The tenant match (`c.organization_id = m.organization_id`)
  is a join filter, applied to the single case row.
- **Estimates.** Estimated rows equal actual rows at every node: 1, with
  `loops=1` throughout. The `actor` scan removes 36 rows by filter, as
  expected. There is no estimation error.
- **Cost profile.** Planning time (0.63–1.23 ms) is about 10 times the
  execution time (0.061–0.148 ms) in every run. Each run is a new backend, so
  planning includes catalog-cache warm-up: 284–321 planning buffer hits against
  5 executor buffers. Because the application also opens a new connection per
  call, planning work is a visible share of this statement's database time.
  This is an inference from these plans; per-request connection cost itself
  was not measured.

**Context: Query A0** ([`raw/query-a0-actor-status.txt`](raw/query-a0-actor-status.txt)):
`Seq Scan on actor`, 1 buffer, rows estimated 1 / actual 1. Execution was
0.049 ms in run 1 (read=1) and 0.019–0.026 ms warm (hit=1). Planning was
0.233–0.439 ms. It runs on the same connection before Query A. **The real
read path is therefore at least two planned and executed statements, plus
connection setup.** Neither timing above is API latency.

## Query B: organization-scoped list (limit 20, fetch 21)

Raw: [`raw/query-b-list-cases-large-org.txt`](raw/query-b-list-cases-large-org.txt)
for `baseline-large`, which holds 40,000 of the 50,000 cases. Statement:
[`sql/query_b_list_cases.sql`](sql/query_b_list_cases.sql).

**Plan (identical in all 6 runs):**

```text
Sort (c.created_at, c.id; quicksort 27kB)                      <- outer ORDER BY, 21 rows
  InitPlan 1: Seq Scan on actor                                <- actor_active scalar
  InitPlan 2: Nested Loop (Seq Scan actor, Seq Scan membership) <- EXISTS, for the authorized output column
  -> Nested Loop Left Join
       -> Result
       -> Limit (21)
            InitPlan 3: Nested Loop (Seq Scan actor, Seq Scan membership)   <- same EXISTS, for the gate
            -> Sort (c.created_at, c.id; top-N heapsort 27kB)
                 -> Result (One-Time Filter: $2)
                      -> Seq Scan on case_record c   Filter: organization_id = $2
```

| Run | Cache state | Planning | Execution | Buffers (whole statement) |
|---|---|---:|---:|---|
| 1 | After restart (PostgreSQL-cold) | 1.501 ms | 98.667 ms | shared hit=9 read=2768 |
| 2 | Warm | 1.761 ms | 40.700 ms | shared hit=2777 |
| 3 | Warm | 1.058 ms | 32.557 ms | shared hit=2777 |
| 4 | Warm | 1.058 ms | 23.414 ms | shared hit=2777 |
| 5 | Warm | 1.432 ms | 45.869 ms | shared hit=2777 |
| 6 | Warm | 1.000 ms | 25.440 ms | shared hit=2777 |

- **Access path.**
  - A sequential scan of the whole `case_record` heap: 2,766 pages, about
    22 MB.
  - It keeps 40,000 rows and removes 10,000 by filter (`Rows Removed by
    Filter: 10000`).
  - The planner did **not** use `case_record_organization_case_key
    (organization_id, id)`. About 80% of rows match, and that index provides
    no `created_at` order, so it would not avoid the sort.
  - There is no parallel plan, although the table exceeds
    `min_parallel_table_scan_size`.
- **Ordering.** A **top-N heapsort** keeps the first 21 of 40,000 rows by
  `(created_at, id)` in 27 kB. The query returns 21 rows (20 shown plus the
  `has_more` probe). An outer quicksort then re-sorts those 21 rows for the
  statement-level `ORDER BY`, which is negligible.
- **Work versus result.** Producing 21 rows reads every page of the table and
  sorts every row of the organization. The work grows with the organization's
  case count (and with table size, for the scan), not with the page size. This
  is the main baseline observation. The cause of the warm-run spread (23–46 ms,
  with identical plans and buffers) was not isolated. It is consistent with
  VM and CPU-quota noise.
- **Authorization structure.**
  - The `EXISTS` decision appears **twice**: InitPlan 2 for the `authorized`
    output column and InitPlan 3 for the gate inside the lateral subquery.
    Each executes once (`loops=1`) and reads 2 buffers. This is the planner's
    expansion of the lateral reference, not a second authorization rule. Its
    cost is negligible here.
  - The case scan sits under `Result (One-Time Filter: $2)`. The plan
    structure therefore skips the `case_record` scan entirely when the
    decision is false. That denied path was not executed in this experiment.
    It is the expected executor behavior, not a measured result.
- **Estimates.** The case scan was estimated at 39,993 rows and returned
  40,000. Limit and sorts were estimated and actual at 21. Every
  identity-table node was 1/1. There is no material estimation error.
- **Buffers.** Run 1 read 2,768 pages from outside `shared_buffers`, with OS
  cache state unknown. Warm runs hit all 2,777 in `shared_buffers`.

### Supplementary: same statement, small organization

Raw: [`raw/query-b-list-cases-small-org-supplementary.txt`](raw/query-b-list-cases-small-org-supplementary.txt)
(`baseline-small`, 1,000 cases, 2%). This is not one of the two required
plans. It shows that the access path depends on the selected organization.

- **Plan (all 6 runs):** `Bitmap Index Scan on
  case_record_organization_case_key` (`organization_id = $2`; 11 buffers),
  then `Bitmap Heap Scan` (`Heap Blocks: exact=1000`), then top-N heapsort to
  21 rows.
- **Estimates:** 995 estimated, 1,000 actual.
- **Timing:** run 1 took 16.395 ms (hit=9, read=1013). Warm runs took
  1.520–3.967 ms (hit=1022).
- **Observation:** the `db/004` unique key, created for the idempotency
  foreign key, already serves as an organization filter index for selective
  organizations.
- **Observation:** 1,000 rows touch 1,000 distinct heap pages, because the
  fixture interleaves organizations. Rows physically clustered by organization
  would touch far fewer pages. This is an inference about this dataset's
  layout, not a measurement.

## Application-path cross-check

Raw: [`raw/app-path-auto-explain.txt`](raw/app-path-auto-explain.txt) and
[`raw/app-path-client-output.txt`](raw/app-path-client-output.txt).

The unmodified `get_case()` and `list_cases(limit=20)` were called once each,
with psycopg-bound parameters (`$4 = '21'` is logged). `auto_explain` (analyze,
buffers, verbose, settings) logged:

| Statement | Plan shape | Logged duration and buffers |
|---|---|---|
| Actor status | Same as Query A0 | 0.034 ms; hit=1 |
| Authorized read | Same as Query A | 0.360 ms; hit=3 read=2 |
| Authorized list | Same as Query B: `Seq Scan` on `case_record`, top-N heapsort; estimate 39,993 | 42.352 ms; hit=1011 read=1766 |

The client received 20 items with `has_more=True`. The `read=1766` for the
list follows the restart before the small-organization pass, which cached only
the ~1,000 pages that pass touched. This confirms that the psql reproduction
plans the application's statements the same way. It is one execution each
under `auto_explain`'s instrumentation overhead, so it adds no timing
evidence.

## What this baseline does and does not show

**Shows:**

- Both authorized statements run as written, with their authorization
  predicates evaluated inside the statement.
- Query A is a primary-key lookup with constant work: 5 buffers.
- Query B for a large organization scans the whole table and sorts every
  matching row to return 21.
- The existing `(organization_id, id)` unique index is chosen only when the
  organization is selective.
- Row estimates are accurate at this size.

**Does not show:**

- Behavior at other data sizes or distributions, under concurrency, or with
  larger identity tables.
- The effect of any index (none was created).
- Tenant-isolation correctness.
- API latency.
- Any production capacity.

## Candidate questions for Week 3 (not decisions)

1. **List ordering.** Would an index matching `WHERE organization_id = ?
   ORDER BY created_at, id` let Query B stop after 21 rows instead of scanning
   and sorting 40,000? What would it cost on insert, and in size, next to the
   existing `(organization_id, id)` key? Could it make that key's
   query-serving role redundant? It cannot replace the key's foreign-key role.
2. **Selectivity crossover.** At which organization share does the planner
   switch between the sequential scan and the bitmap scan on
   `(organization_id, id)`? Is either path stable for the medium organization
   (18%)? Its plan was not captured here.
3. **Deeper pages.** With cursor pagination (`created_at, id > cursor`), how
   do these plans change for later pages, compared with this first-page
   baseline?
4. **Identity lookups at scale.** At what `organization_membership` and
   `actor` sizes do the primary-key indexes replace the one-page sequential
   scans? Does any listing query need `organization_membership(organization_id)`?
5. **Planning share.** With a new connection per call, planning (about 1 ms)
   exceeds Query A's execution (about 0.1 ms). Is connection reuse,
   statement preparation or pooling in scope later, and how should
   end-to-end read latency be measured separately from these plans?
6. **Layout sensitivity.** How much does physical clustering by organization
   change heap pages per query, compared with this interleaved fixture?
7. **Measurement design.** Reuse this fixture and method as the "before"
   state. Add a second documented size, more repetitions, and a recorded
   cache protocol. Report raw observations, not single-run differences.
