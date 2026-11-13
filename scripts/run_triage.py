"""Run one synthetic-case triage experiment and persist the suggestion only."""

from __future__ import annotations

import argparse
import json
import os
import platform
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from src.model_adapter import INSTRUCTIONS, PROMPT_VERSION, request_triage

CASE_TITLE = "Employees cannot connect to VPN"
CASE_ORG_SLUG = "northwind"


def read_ticket(dsn: str) -> dict:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.id, c.title, c.description, c.status, c.version,
                       o.slug AS organization_slug
                FROM case_record AS c
                JOIN organization AS o ON o.id = c.organization_id
                WHERE o.slug = %s AND c.title = %s
                ORDER BY c.created_at ASC, c.id ASC
                LIMIT 1
                """,
                (CASE_ORG_SLUG, CASE_TITLE),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Expected synthetic seed case was not found; run db/seed_dev.sql")
            return {
                "id": str(row["id"]),
                "organization_slug": row["organization_slug"],
                "title": row["title"],
                "description": row["description"],
                "status": row["status"],
                "version": row["version"],
            }


def insert_suggestion_only(dsn: str, ticket: dict, suggestion, model_id: str) -> tuple[str, dict]:
    """Verify case unchanged, insert proposal, confirm case status unchanged."""
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, version FROM case_record WHERE id = %s FOR SHARE",
                (ticket["id"],),
            )
            current = cursor.fetchone()
            if current is None or current["version"] != ticket["version"] or current["status"] != ticket["status"]:
                raise RuntimeError("Case was changed while the model was running: stale suggestion not stored")

            cursor.execute(
                """
                INSERT INTO ai_suggestion (
                    case_id, case_version, proposed_category, proposed_urgency,
                    reasoning, suggested_next_step, model_id, prompt_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    ticket["id"], ticket["version"], suggestion.category,
                    suggestion.urgency, suggestion.reason,
                    suggestion.suggested_next_step, model_id, PROMPT_VERSION,
                ),
            )
            suggestion_id = str(cursor.fetchone()["id"])
            cursor.execute("SELECT status, version FROM case_record WHERE id = %s", (ticket["id"],))
            after = dict(cursor.fetchone())
            if after != current:
                raise RuntimeError("Unexpected case state change during suggestion persistence")
    return suggestion_id, after


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one real-model triage experiment")
    parser.add_argument("--evidence", default="evaluations/model-run-01.json")
    args = parser.parse_args()
    load_dotenv()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY in your ignored local .env before running")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set DATABASE_URL in your ignored local .env before running")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    evidence_path = Path(args.evidence)
    if evidence_path.exists():
        raise SystemExit(f"Evidence file already exists: {evidence_path} (choose another path)")

    ticket = read_ticket(dsn)
    suggestion, response, elapsed_ms = request_triage(ticket, model)
    actual_model = response.model or model
    suggestion_id, case_after = insert_suggestion_only(dsn, ticket, suggestion, actual_model)

    usage = response.usage
    evidence = {
        "experiment": "ai-triage-structured-output",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": "openai",
        "requested_model": model,
        "actual_model": actual_model,
        "prompt_version": PROMPT_VERSION,
        "prompt_instructions": INSTRUCTIONS.strip(),
        "synthetic_input": ticket,
        "actual_model_output_text": response.output_text,
        "validated_recommendation": suggestion.model_dump(),
        "response_status": response.status,
        "validation_status": "passed",
        "elapsed_ms": elapsed_ms,
        "token_usage": None if usage is None else {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.system(),
            "openai_sdk": package_version("openai"),
            "postgresql": "local Docker container (server version not queried by this script)",
        },
        "database": {
            "table_written": "ai_suggestion",
            "suggestion_id": suggestion_id,
            "case_status_before": ticket["status"],
            "case_status_after": case_after["status"],
            "case_version_before": ticket["version"],
            "case_version_after": case_after["version"],
            "case_state_unchanged_at_write": (
                ticket["status"] == case_after["status"]
                and ticket["version"] == case_after["version"]
            ),
        },
        "scope_note": "Single synthetic-case run; does not validate tenant authorization or model accuracy",
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Validated suggestion saved to ai_suggestion: {suggestion_id}")
    print(f"Case state unchanged during persistence: {evidence['database']['case_state_unchanged_at_write']}")
    print(f"Observed elapsed_ms: {elapsed_ms}")
    print(f"Evidence saved to: {evidence_path}")


if __name__ == "__main__":
    main()
