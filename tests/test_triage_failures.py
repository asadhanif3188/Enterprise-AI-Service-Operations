"""Deterministic AI-triage failure-path tests; never call a hosted model.

Unit tests: run without a database. Database tests: require TEST_DATABASE_URL
pointing to a dedicated database named eaiso_test, with db/001_init.sql applied.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.model_adapter import TriageSuggestion, request_triage


VALID_OUTPUT = {
    "category": "network",
    "urgency": "medium",
    "reason": "Several synthetic users report VPN connectivity failures.",
    "suggested_next_step": "Inspect the VPN gateway's connectivity logs.",
}

SYNTHETIC_TICKET = {
    "id": "00000000-0000-0000-0000-000000000001",
    "organization_slug": "northwind",
    "title": "Employees cannot connect to VPN",
    "description": "Multiple users report VPN connection failures.",
    "status": "NEW",
    "version": 1,
}


def test_missing_output_field_rejected():
    data = {k: v for k, v in VALID_OUTPUT.items() if k != "reason"}
    with pytest.raises(ValidationError):
        TriageSuggestion.model_validate(data)


def test_unknown_urgency_rejected():
    data = dict(VALID_OUTPUT, urgency="emergency")
    with pytest.raises(ValidationError):
        TriageSuggestion.model_validate(data)


def test_blank_next_step_rejected():
    data = dict(VALID_OUTPUT, suggested_next_step="  \n ")
    with pytest.raises(ValidationError):
        TriageSuggestion.model_validate(data)


def test_unexpected_approval_action_rejected():
    data = dict(VALID_OUTPUT, approve=True, status="CLOSED")
    with pytest.raises(ValidationError):
        TriageSuggestion.model_validate(data)


def _replace_provider(monkeypatch, *, result=None, error=None):
    """Replace the SDK network boundary with a deterministic fake."""
    import openai

    observed = {"calls": 0, "settings": None, "args": None}

    class Responses:
        def parse(self, **kwargs):
            observed["calls"] += 1
            observed["args"] = kwargs
            if error is not None:
                raise error
            return result

    class Client:
        def __init__(self, **kwargs):
            observed["settings"] = kwargs
            self.responses = Responses()

    monkeypatch.setattr(openai, "OpenAI", Client)
    return observed


def test_provider_timeout_is_propagated_without_retries(monkeypatch):
    observed = _replace_provider(monkeypatch, error=TimeoutError("simulated provider timeout"))
    with pytest.raises(TimeoutError, match="provider timeout"):
        request_triage(SYNTHETIC_TICKET, "synthetic-provider-model")
    assert observed["calls"] == 1
    assert observed["settings"]["max_retries"] == 0
    assert observed["settings"]["timeout"] == 30.0
    assert observed["args"]["store"] is False
    assert observed["args"]["text_format"] is TriageSuggestion
    assert json.loads(observed["args"]["input"][0]["content"])["status"] == "NEW"


def test_provider_refusal_is_not_a_usable_suggestion(monkeypatch):
    response = SimpleNamespace(status="completed", output_parsed=None)
    _replace_provider(monkeypatch, result=response)
    with pytest.raises(RuntimeError, match="no usable structured recommendation"):
        request_triage(SYNTHETIC_TICKET, "synthetic-provider-model")


def test_incomplete_model_response_is_rejected(monkeypatch):
    response = SimpleNamespace(status="incomplete", output_parsed=None)
    _replace_provider(monkeypatch, result=response)
    with pytest.raises(RuntimeError, match="incomplete"):
        request_triage(SYNTHETIC_TICKET, "synthetic-provider-model")


@pytest.mark.parametrize("exception", [TimeoutError("simulated timeout"), ConnectionError("simulated network failure")])
def test_cli_dependency_failure_writes_neither_suggestion_nor_evidence(monkeypatch, tmp_path, exception):
    from scripts import run_triage as runner

    evidence = tmp_path / "failed-run.json"
    observed = {"write_calls": 0}
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-only")
    monkeypatch.setenv("DATABASE_URL", "test-dsn-not-used")
    monkeypatch.setattr(runner, "read_ticket", lambda dsn: dict(SYNTHETIC_TICKET))

    def failing_provider(ticket, model):
        raise exception

    def forbidden_write(*args, **kwargs):
        observed["write_calls"] += 1
        pytest.fail("No persistence may happen when inference fails")

    monkeypatch.setattr(runner, "request_triage", failing_provider)
    monkeypatch.setattr(runner, "insert_suggestion_only", forbidden_write)
    monkeypatch.setattr(sys, "argv", ["run_triage", "--evidence", str(evidence)])

    with pytest.raises(type(exception)):
        runner.main()
    assert observed["write_calls"] == 0
    assert not evidence.exists()


def test_existing_evidence_path_stops_before_model_call(monkeypatch, tmp_path):
    from scripts import run_triage as runner

    evidence = tmp_path / "previous-experiment.json"
    evidence.write_text("{\"original\": true}\n", encoding="utf-8")
    original = evidence.read_text(encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-only")
    monkeypatch.setenv("DATABASE_URL", "test-dsn-not-used")
    monkeypatch.setattr(runner, "read_ticket", lambda *_: pytest.fail("Must stop before database read"))
    monkeypatch.setattr(runner, "request_triage", lambda *_: pytest.fail("Must not call model"))
    monkeypatch.setattr(sys, "argv", ["run_triage", "--evidence", str(evidence)])

    with pytest.raises(SystemExit, match="Evidence file already exists"):
        runner.main()
    assert evidence.read_text(encoding="utf-8") == original


# ----- Real PostgreSQL tests (dedicated eaiso_test database only) -------------

@pytest.fixture
def test_db_dsn():
    """Skip unless explicitly configured; refuse any non-test database."""
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for the dedicated eaiso_test database")

    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    dbname = conninfo_to_dict(dsn).get("dbname")
    if dbname != "eaiso_test":
        pytest.fail("Safety guard: TEST_DATABASE_URL must use database eaiso_test")

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            actual = cur.fetchone()[0]
    if actual != "eaiso_test":
        pytest.fail("Safety guard: connected database is not eaiso_test")
    return dsn


@pytest.fixture
def fresh_case(test_db_dsn):
    """Insert a unique test org and case, then remove both after each test."""
    import psycopg
    from psycopg.rows import dict_row

    slug = "failure-test-" + uuid4().hex
    with psycopg.connect(test_db_dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organization (name, slug) VALUES (%s, %s) RETURNING id",
                ("Synthetic failure-path organization", slug),
            )
            org_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO case_record (organization_id, title, description) "
                "VALUES (%s, %s, %s) RETURNING id, status, version",
                (org_id, "Synthetic failing VPN case", "Fictional connectivity issue"),
            )
            row = cur.fetchone()
            ticket = {
                "id": str(row["id"]),
                "organization_slug": slug,
                "title": "Synthetic failing VPN case",
                "description": "Fictional connectivity issue",
                "status": row["status"],
                "version": row["version"],
            }

    try:
        yield test_db_dsn, ticket
    finally:
        # This touches only a disposable test database and the unique test rows.
        with psycopg.connect(test_db_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ai_suggestion WHERE case_id = %s", (ticket["id"],))
                cur.execute("DELETE FROM case_record WHERE id = %s", (ticket["id"],))
                cur.execute("DELETE FROM organization WHERE id = %s", (org_id,))


def _db_state(dsn, case_id):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, version FROM case_record WHERE id = %s", (case_id,))
            state = dict(cur.fetchone())
            cur.execute("SELECT COUNT(*) AS n FROM ai_suggestion WHERE case_id = %s", (case_id,))
            count = cur.fetchone()["n"]
    return state, count


@pytest.mark.db
def test_real_database_accepts_suggestion_without_changing_case(fresh_case):
    from scripts.run_triage import insert_suggestion_only

    dsn, ticket = fresh_case
    suggestion = TriageSuggestion.model_validate(VALID_OUTPUT)
    suggestion_id, after = insert_suggestion_only(dsn, ticket, suggestion, "synthetic-test-model")
    assert suggestion_id
    assert after == {"status": "NEW", "version": 1}
    state, count = _db_state(dsn, ticket["id"])
    assert state == {"status": "NEW", "version": 1}
    assert count == 1


@pytest.mark.db
def test_stale_case_revision_rejected_without_insert(fresh_case):
    import psycopg
    from scripts.run_triage import insert_suggestion_only

    dsn, ticket = fresh_case
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE case_record SET version = version + 1 WHERE id = %s", (ticket["id"],))

    with pytest.raises(RuntimeError, match="stale suggestion not stored"):
        insert_suggestion_only(dsn, ticket, TriageSuggestion.model_validate(VALID_OUTPUT), "synthetic-test-model")
    state, count = _db_state(dsn, ticket["id"])
    assert state == {"status": "NEW", "version": 2}
    assert count == 0


@pytest.mark.db
def test_changed_case_status_rejected_without_insert(fresh_case):
    import psycopg
    from scripts.run_triage import insert_suggestion_only

    dsn, ticket = fresh_case
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE case_record SET status = 'IN_REVIEW' WHERE id = %s", (ticket["id"],))

    with pytest.raises(RuntimeError, match="stale suggestion not stored"):
        insert_suggestion_only(dsn, ticket, TriageSuggestion.model_validate(VALID_OUTPUT), "synthetic-test-model")
    state, count = _db_state(dsn, ticket["id"])
    assert state == {"status": "IN_REVIEW", "version": 1}
    assert count == 0


@pytest.mark.db
def test_database_constraint_error_rolls_back_failed_insert(fresh_case):
    import psycopg
    from scripts.run_triage import insert_suggestion_only

    dsn, ticket = fresh_case
    # The database rejects a blank model_id.  The calling transaction fails;
    # no suggestion is committed, and the case is left as it was.
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_suggestion_only(dsn, ticket, TriageSuggestion.model_validate(VALID_OUTPUT), "")
    state, count = _db_state(dsn, ticket["id"])
    assert state == {"status": "NEW", "version": 1}
    assert count == 0
