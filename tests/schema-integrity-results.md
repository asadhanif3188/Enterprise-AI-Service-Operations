# PostgreSQL Schema Integrity Validation Report

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Run date:** 2026-11-12 (local development run)  
**Result:** Passed — 11 of 11 checks  
**Scope:** Relational integrity of the initial schema in [`db/001_init.sql`](../db/001_init.sql)

## Test environment

| Item | Value |
|---|---|
| Environment | Local Windows development environment; PostgreSQL running in Docker |
| PostgreSQL server | PostgreSQL 16.14 (Debian 16.14-1.pgdg13+1), from `SELECT version();` |
| Container | `eaiso-postgres` |
| Database | `eaiso_dev` |
| Database user | `eaiso_dev` |
| Schema under test | [`db/001_init.sql`](../db/001_init.sql) |
| Development data | [`db/seed_dev.sql`](../db/seed_dev.sql) |
| Test script | [`tests/test_schema_integrity.sql`](test_schema_integrity.sql) |

The schema migration and synthetic seed data were applied before the test run. The test script assumes both are present.

## Reproduction

From the repository root in PowerShell, with the PostgreSQL container running and the migration and seed data already applied:

```powershell
docker cp tests/test_schema_integrity.sql eaiso-postgres:/tmp/test_schema_integrity.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -v ON_ERROR_STOP=1 -f /tmp/test_schema_integrity.sql
```

The `-v ON_ERROR_STOP=1` option makes `psql` stop on an unexpected SQL error and return a nonzero exit code. Expected constraint violations are caught inside the test script, which checks that each rejection is the intended one (for `CHECK` constraints, by constraint name).

## Checks

| ID | Check | Result |
|---|---|---|
| 01 | Seed organizations and cases are present for both fictional customers | PASS |
| 02 | Valid organization, case, and suggestion inserts succeed; case defaults to `NEW` / version `1`; adding a suggestion leaves case state unchanged | PASS |
| 03 | Invalid case status is rejected | PASS |
| 04 | Missing `organization_id` is rejected | PASS |
| 05 | Foreign key to a nonexistent organization is rejected | PASS |
| 06 | Duplicate organization slug is rejected | PASS |
| 07 | Blank case title is rejected | PASS |
| 08 | Nonpositive case version is rejected | PASS |
| 09 | Suggestion referencing a nonexistent case is rejected | PASS |
| 10 | Nonpositive suggestion `case_version` is rejected | PASS |
| 11 | Deleting an organization referenced by a case is rejected | PASS |

## Observed output

Output from running the current test script, with the script supplied to `psql` on standard input:

```text
BEGIN
psql:<stdin>:156: NOTICE:  PASS 01: synthetic seed organizations and cases exist
psql:<stdin>:156: NOTICE:  PASS 02: valid organization, case, suggestion, defaults, and unchanged case state
psql:<stdin>:156: NOTICE:  PASS 03: invalid case status rejected
psql:<stdin>:156: NOTICE:  PASS 04: missing organization_id rejected
psql:<stdin>:156: NOTICE:  PASS 05: unknown organization reference rejected
psql:<stdin>:156: NOTICE:  PASS 06: duplicate organization slug rejected
psql:<stdin>:156: NOTICE:  PASS 07: blank case title rejected
psql:<stdin>:156: NOTICE:  PASS 08: nonpositive case version rejected
psql:<stdin>:156: NOTICE:  PASS 09: unknown suggestion case reference rejected
psql:<stdin>:156: NOTICE:  PASS 10: nonpositive suggestion case_version rejected
DO
psql:<stdin>:156: NOTICE:  PASS 11: referenced organization deletion rejected
psql:<stdin>:156: NOTICE:  ALL 11 SCHEMA INTEGRITY CHECKS PASSED
ROLLBACK
```

`psql` exited with code `0`. The position of the `DO` line reflects interleaving of the standard output and notice streams, not the order of execution.

An earlier run on the same date used a previous revision of the script that differed only in its probe names and final notice label. It also reported 11 of 11 checks passed.

## Data cleanup

The test runs in a single transaction that ends with `ROLLBACK`, so its probe rows, including the valid inserts in check 02, are discarded. A query after the run confirmed that no probe organization remained. The development seed records are not modified.

## Interpretation

These results demonstrate the tested **basic relational integrity** behavior of the initial schema: required ownership, valid status values, positive revisions, foreign-key references, slug uniqueness, title validation, restricted deletion, and storage of a non-authoritative AI suggestion without changing case state.

They **do not** demonstrate tenant authorization or isolation, allowed lifecycle transitions, automatic version increments, optimistic concurrency, approval or audit history, or the quality and safety of real model output. Those require separate implementation and verification.

## Outcome

**Relational integrity validation of the initial schema: passed (11/11).**

This report covers database integrity only. A separate initial AI triage integration and its first recorded experiment are documented in [`docs/ai-triage-integration.md`](../docs/ai-triage-integration.md) and [`evaluations/model-run-01.json`](../evaluations/model-run-01.json). That experiment is a separate result and is not part of these 11 checks.
