"""Supplementary: run the real get_case() and list_cases() once each.

The primary baseline plans come from psql (sql/query_*.sql). This script calls
the unmodified ``AuthorizedCaseOperations`` methods against the experiment
database, so that auto_explain (enabled for that database only by
run_baseline.sh) logs the plans PostgreSQL chose for the statements exactly as
the application sends them through psycopg. It is a cross-check that the psql
reproduction matches the application path, not an extra measurement.

Identity: a stub resolver returns the synthetic actor directly. Credential
verification is deliberately outside this experiment; the authorization
predicates inside both statements are unchanged.

Requires BASELINE_DSN in the environment. Run from the repository root.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.authorized_case_operations import AuthorizedCaseOperations  # noqa: E402
from src.credential_auth import VerifiedActor  # noqa: E402

ACTOR_ID = UUID("1a1e8387-9f2a-572f-3ade-0f8211c630f8")  # md5('eaiso-day14-actor-1')
CASE_ID = "7dd97bf9-c05f-8ef8-ab05-3809f2576dc7"  # md5('eaiso-day14-case-25000')
ORG_LARGE = "8af08632-4147-4fce-49b8-0f34277f37f1"  # md5('eaiso-day14-org-1')


class StubResolver:
    """Returns the fixed synthetic actor; experiment use only."""

    def resolve(self, credential: object) -> VerifiedActor:
        return VerifiedActor(actor_id=ACTOR_ID, credential_id="baseline-stub")


def main() -> int:
    dsn = os.environ["BASELINE_DSN"]
    ops = AuthorizedCaseOperations(StubResolver(), dsn)
    case = ops.get_case(object(), CASE_ID)
    page = ops.list_cases(object(), ORG_LARGE, limit=20)
    print(f"get_case: returned case in organization {case['organization_id']}")
    print(f"list_cases: {len(page['items'])} items, has_more={page['has_more']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
