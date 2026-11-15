"""Run the local case API on the loopback interface only.

    python -m scripts.run_local_api [--port 8000]

Binds to 127.0.0.1; there is deliberately no option to bind another address.
Reads DATABASE_URL (and optionally EAISO_CREDENTIALS_FILE) from the
environment or the local, git-ignored .env file. For a controlled local
demonstration with synthetic data and test credentials only: the database
connection may be a superuser and PostgreSQL row-level security is absent.
"""

from __future__ import annotations

import argparse

import uvicorn
from dotenv import load_dotenv

LOOPBACK = "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local case API on 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Choose an unprivileged port (1024-65535)")
    load_dotenv()
    uvicorn.run("src.api.app:app", host=LOOPBACK, port=args.port, proxy_headers=False,
                server_header=False, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
