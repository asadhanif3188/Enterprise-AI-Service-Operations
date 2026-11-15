"""Provision the fictional identity scenario into a LOCAL development or test database.

Trusted local operator tool only. It is not an authenticated or authorized
service, it exposes no network endpoint, and it creates no credentials.

    python -m scripts.provision_dev_identities --database-url postgresql://...

Without --database-url the DATABASE_URL from the local .env is used. The tool
refuses any database other than eaiso_dev or eaiso_test, and any host other
than the local machine. Rerunning is safe: existing matching actors and grants
are left unchanged, and any difference is reported instead of overwritten.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict

from src import identity_provisioning as provisioning

ALLOWED_DATABASES = frozenset({"eaiso_dev", "eaiso_test"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def check_local_target(dsn: str) -> str:
    """Return the database name, or raise SystemExit for a non-local target."""
    params = conninfo_to_dict(dsn)
    # hostaddr and service files can redirect the connection away from "host".
    redirects = (params.get("hostaddr"), params.get("service"),
                 os.getenv("PGHOSTADDR"), os.getenv("PGSERVICE"))
    if any(redirects):
        raise SystemExit("Refusing hostaddr/service connection settings: use host=127.0.0.1")
    dbname = params.get("dbname")
    hosts = [h for h in str(params.get("host") or "").split(",") if h]
    if dbname not in ALLOWED_DATABASES:
        raise SystemExit(f"Refusing database {dbname!r}: only {sorted(ALLOWED_DATABASES)} are allowed")
    if not hosts or any(h not in LOCAL_HOSTS for h in hosts):
        raise SystemExit("Refusing non-local host: provisioning is for the local machine only")
    return dbname


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision fictional local actors and grants")
    parser.add_argument("--database-url", help="Local eaiso_dev or eaiso_test connection string")
    parser.add_argument("--org-a-slug", default="northwind", help="Organization A (default: northwind)")
    parser.add_argument("--org-b-slug", default="summit", help="Organization B (default: summit)")
    args = parser.parse_args(argv)

    load_dotenv()
    dsn = args.database_url or os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set --database-url or DATABASE_URL")
    expected = check_local_target(dsn)

    with provisioning.connect(dsn) as conn:
        actual = conn.execute("SELECT current_database()").fetchone()[0]
        if actual != expected:
            raise SystemExit(f"Connected database {actual!r} differs from requested {expected!r}")
        try:
            result = provisioning.provision_synthetic_scenario(
                conn, org_a_slug=args.org_a_slug, org_b_slug=args.org_b_slug
            )
        except provisioning.ProvisioningError as exc:
            print(f"Provisioning stopped: {exc}. Steps completed before this point "
                  "remain committed; resolve the problem and rerun.", file=sys.stderr)
            return 1
        violations = provisioning.eligibility_violations(conn)

    print(f"Database: {actual}")
    print(f"Actors created: {', '.join(result.created_actors) or 'none (already present)'}")
    print("Grants created: "
          + (", ".join(f"{a}:{o}:{r}" for a, o, r in result.created_grants) or "none (already present)"))
    for key, actor_id in result.actor_ids.items():
        print(f"  {key}: {actor_id}")
    print(f"Active grants violating eligibility: {len(violations)}")
    return 0 if not violations else 2


if __name__ == "__main__":
    sys.exit(main())
