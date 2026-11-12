# Day 4 — PostgreSQL Schema Integrity Test Results

**Project:** Enterprise AI Service Operations & Intelligence Platform  
**Test date:** 2026-11-12 (local development run)  
**Status:** Passed — 11 of 11 checks reported as successful  
**Evidence source:** Terminal output from the local Docker/`psql` run, supplied by the project maintainer

## Test environment

| Item | Value |
|---|---|
| Environment | Local Windows development environment; PostgreSQL running in Docker |
| Container | `eaiso-postgres` |
| Database | `eaiso_dev` |
| Database user | `eaiso_dev` |
| Schema under test | `db/001_init.sql` |
| Development data | `db/seed_dev.sql` |
| Test script | `tests/test_schema_integrity.sql` |
| PostgreSQL server version | Not captured in the supplied test output |

The schema migration and synthetic seed data were applied before the test run. The test script assumes both are present.

## Reproduction

From the repository root, with the PostgreSQL container running and the migration and seed data already applied:

```powershell
docker cp tests/test_schema_integrity.sql eaiso-postgres:/tmp/test_schema_integrity.sql
docker exec -it eaiso-postgres psql -U eaiso_dev -d eaiso_dev -v ON_ERROR_STOP=1 -f /tmp/test_schema_integrity.sql
```

The `-v ON_ERROR_STOP=1` option instructs `psql` to stop on an unexpected SQL error. Expected constraint violations are handled within the test script, where the specific rejection is checked.

## Observed results

| ID | Check | Observed result |
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

The final output reported:

```text
NOTICE:  PASS 01: synthetic seed organizations and cases exist
NOTICE:  PASS 02: valid organization, case, suggestion, defaults, and unchanged case state
NOTICE:  PASS 03: invalid case status rejected
NOTICE:  PASS 04: missing organization_id rejected
NOTICE:  PASS 05: unknown organization reference rejected
NOTICE:  PASS 06: duplicate organization slug rejected
NOTICE:  PASS 07: blank case title rejected
NOTICE:  PASS 08: nonpositive case version rejected
NOTICE:  PASS 09: unknown suggestion case reference rejected
NOTICE:  PASS 10: nonpositive suggestion case_version rejected
NOTICE:  PASS 11: referenced organization deletion rejected
NOTICE:  ALL 11 DAY 4 DATABASE INTEGRITY CHECKS PASSED
DO
ROLLBACK
```

The command returned to the terminal without a displayed error. A numerical shell exit code was not captured separately.

## Data cleanup and interpretation

The test runs in a transaction and ends with `ROLLBACK`. The valid probe inserts used during testing are therefore discarded. The existing development seed records remain unchanged by those probe inserts.

These results demonstrate the tested **basic relational integrity** behaviors of the initial schema: required ownership, valid status values, positive revisions, foreign-key references, uniqueness, title validation, restricted deletion, and successful storage of a non-authoritative AI suggestion.

They **do not** demonstrate tenant authorization or isolation, allowed lifecycle transitions, automatic version increments, optimistic concurrency, complete approval/audit history, or quality and safety of real model output. Those require separate implementation and verification.

## Outcome and next step

**Day 4 database-integrity evidence gate: passed (11/11 observed checks).**

Commit this report alongside `tests/test_schema_integrity.sql`, then update the README checklist to mark the database integrity tests complete. The next planned activity is Day 5: a real-model triage experiment with structured-output validation and recorded provenance.
