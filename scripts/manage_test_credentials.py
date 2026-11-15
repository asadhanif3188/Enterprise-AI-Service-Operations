"""Manage local, test-only opaque credentials (trusted local operator tool).

    python -m scripts.manage_test_credentials issue  --actor-id <uuid> [--database-url ...]
    python -m scripts.manage_test_credentials rotate --credential-id <id>
    python -m scripts.manage_test_credentials revoke --credential-id <id>
    python -m scripts.manage_test_credentials remove --credential-id <id>
    python -m scripts.manage_test_credentials list

The store file (default .local/test-credentials.json, or EAISO_CREDENTIALS_FILE,
or --credentials-file) is git-ignored and holds only SHA-256 digests mapped to
actor IDs. A raw credential is printed exactly once, by issue or rotate, and
is never written to disk by this tool. Treat that output as a secret: do not
paste it into files under version control, issues, or logs.

This is not an authenticated service and has no network endpoint. Anyone who
can run it, or edit the store file, controls which actor a credential names.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID

import psycopg
from dotenv import load_dotenv

from scripts.provision_dev_identities import check_local_target
from src import credential_auth as auth


def _actor_status(dsn: str, actor_id: UUID) -> bool | None:
    check_local_target(dsn)
    return auth.PostgresActorLookup(dsn)(actor_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage local test-only credentials")
    parser.add_argument("--credentials-file", type=Path,
                        help=f"Store path (default: ${auth.STORE_PATH_ENV} or {auth.DEFAULT_STORE_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="Issue a new credential for an existing actor")
    issue.add_argument("--actor-id", required=True, type=UUID)
    issue.add_argument("--database-url", help="Local eaiso_dev or eaiso_test URL (default: DATABASE_URL)")
    for name in ("rotate", "revoke", "remove"):
        p = sub.add_parser(name)
        p.add_argument("--credential-id", required=True)
    sub.add_parser("list", help="List credential IDs, actors, and status (never secrets)")
    args = parser.parse_args(argv)

    load_dotenv()
    path = args.credentials_file or auth.store_path_from_env()

    try:
        if args.command == "issue":
            dsn = args.database_url or os.getenv("DATABASE_URL")
            if not dsn:
                raise SystemExit("Set --database-url or DATABASE_URL to confirm the actor exists")
            try:
                status = _actor_status(dsn, args.actor_id)
            except psycopg.Error as exc:
                # Generic message: driver errors can name hosts, users, or settings.
                print(f"Refused: database unavailable ({type(exc).__name__})", file=sys.stderr)
                return 1
            if status is None:
                print("Refused: no actor with that ID exists", file=sys.stderr)
                return 1
            credential, entry = auth.issue_credential(path, args.actor_id)
            _print_secret(credential, entry, inactive=status is False)
        elif args.command == "rotate":
            credential, entry = auth.rotate_credential(path, args.credential_id)
            print(f"Revoked credential {args.credential_id}.")
            _print_secret(credential, entry, inactive=False)
        elif args.command == "revoke":
            entry = auth.revoke_credential(path, args.credential_id)
            print(f"Credential {entry.credential_id} revoked (actor {entry.actor_id}).")
        elif args.command == "remove":
            auth.remove_credential(path, args.credential_id)
            print(f"Credential {args.credential_id} removed.")
        else:
            entries = auth.read_store(path) if path.exists() else ()
            for e in entries:
                print(f"{e.credential_id}  actor={e.actor_id}  status={e.status}  created={e.created_at}"
                      + (f"  revoked={e.revoked_at}" if e.revoked_at else ""))
            if not entries:
                print("No credentials.")
    except auth.CredentialConfigurationError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    return 0


def _print_secret(credential: str, entry: auth.CredentialEntry, *, inactive: bool) -> None:
    print(f"Credential {entry.credential_id} issued for actor {entry.actor_id}.")
    if inactive:
        print("Warning: this actor is inactive; the credential will be refused (403 equivalent).")
    print("Shown once; it is not stored anywhere. Keep it out of version control:")
    print(credential)


if __name__ == "__main__":
    sys.exit(main())
