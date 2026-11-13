"""Case service tests. DB tests use only the dedicated eaiso_test database."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from src import case_service as service


# Policy checks are independent of a running database.
@pytest.mark.parametrize("source,target", [
    ("NEW", "IN_REVIEW"),
    ("IN_REVIEW", "RESOLVED"),
    ("RESOLVED", "CLOSED"),
])
def test_forward_policy(source, target):
    service.check_transition(source, target)


@pytest.mark.parametrize("source,target", [
    ("NEW", "RESOLVED"),
    ("NEW", "NEW"),
    ("CLOSED", "IN_REVIEW"),
    ("IN_REVIEW", "NEW"),
    ("NEW", "UNRECOGNIZED"),
])
def test_invalid_policy(source, target):
    with pytest.raises(service.InvalidTransition):
        service.check_transition(source, target)


@pytest.fixture
def case_db_dsn():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for eaiso_test; database cases not executed")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    if conninfo_to_dict(dsn).get("dbname") != "eaiso_test":
        pytest.fail("Safety guard: TEST_DATABASE_URL must refer to eaiso_test")
    with psycopg.connect(dsn) as conn:
        name, history_table = conn.execute(
            "SELECT current_database(), to_regclass('public.case_status_event')"
        ).fetchone()
        if name != "eaiso_test":
            pytest.fail("Safety guard: actual connected database is not eaiso_test")
        if history_table is None:
            pytest.fail("Apply db/002_case_status_events.sql to eaiso_test first")
    return dsn


@pytest.fixture
def test_org(case_db_dsn):
    import psycopg

    slug = "case-service-test-" + uuid4().hex
    with psycopg.connect(case_db_dsn) as conn:
        org_id = conn.execute(
            "INSERT INTO organization(name,slug) VALUES (%s,%s) RETURNING id",
            ("Fictional transition-test organization", slug),
        ).fetchone()[0]
    try:
        yield case_db_dsn, str(org_id)
    finally:
        with psycopg.connect(case_db_dsn) as conn:
            conn.execute(
                """DELETE FROM case_status_event WHERE case_id IN
                     (SELECT id FROM case_record WHERE organization_id=%s)""", (org_id,)
            )
            conn.execute(
                """DELETE FROM ai_suggestion WHERE case_id IN
                     (SELECT id FROM case_record WHERE organization_id=%s)""", (org_id,)
            )
            conn.execute("DELETE FROM case_record WHERE organization_id=%s", (org_id,))
            conn.execute("DELETE FROM organization WHERE id=%s", (org_id,))


def new_case(test_org):
    dsn, org_id = test_org
    return service.create_case(dsn, org_id, "Synthetic VPN incident", "Fictional issue")


def event_rows(dsn, case_id):
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute(
            """SELECT from_status, to_status, from_version, to_version, actor_label
               FROM case_status_event WHERE case_id=%s ORDER BY to_version""",
            (case_id,),
        ).fetchall()


@pytest.mark.db
def test_create_and_read_case(test_org):
    dsn, org_id = test_org
    created = new_case(test_org)
    assert (created["status"], created["version"]) == ("NEW", 1)
    read = service.get_case(dsn, str(created["id"]), org_id)
    assert read["id"] == created["id"]
    assert event_rows(dsn, created["id"]) == []


@pytest.mark.db
def test_entire_forward_lifecycle_and_event_history(test_org):
    dsn, org_id = test_org
    case = new_case(test_org)
    for expected_version, target in enumerate(("IN_REVIEW", "RESOLVED", "CLOSED"), start=1):
        case = service.transition_case(
            dsn, str(case["id"]), org_id, expected_version=expected_version,
            target_status=target, actor_label="local-synthetic-operator",
        )
        assert case["status"] == target
        assert case["version"] == expected_version + 1
    assert [row[:4] for row in event_rows(dsn, case["id"])] == [
        ("NEW", "IN_REVIEW", 1, 2),
        ("IN_REVIEW", "RESOLVED", 2, 3),
        ("RESOLVED", "CLOSED", 3, 4),
    ]


@pytest.mark.db
def test_skip_transition_rejected_without_write(test_org):
    dsn, org_id = test_org
    case = new_case(test_org)
    with pytest.raises(service.InvalidTransition):
        service.transition_case(
            dsn, str(case["id"]), org_id, expected_version=1,
            target_status="RESOLVED", actor_label="local-synthetic-operator",
        )
    assert service.get_case(dsn, str(case["id"]), org_id)["version"] == 1
    assert event_rows(dsn, case["id"]) == []


@pytest.mark.db
def test_stale_version_rejected_without_second_event(test_org):
    dsn, org_id = test_org
    case = new_case(test_org)
    service.transition_case(
        dsn, str(case["id"]), org_id, expected_version=1,
        target_status="IN_REVIEW", actor_label="local-synthetic-operator",
    )
    with pytest.raises(service.VersionConflict):
        service.transition_case(
            dsn, str(case["id"]), org_id, expected_version=1,
            target_status="IN_REVIEW", actor_label="local-synthetic-operator",
        )
    assert service.get_case(dsn, str(case["id"]), org_id)["version"] == 2
    assert len(event_rows(dsn, case["id"])) == 1


@pytest.mark.db
def test_wrong_organization_scope_cannot_read_or_update(test_org):
    dsn, org_id = test_org
    case = new_case(test_org)
    wrong_org = str(uuid4())
    with pytest.raises(service.CaseNotFound):
        service.get_case(dsn, str(case["id"]), wrong_org)
    with pytest.raises(service.CaseNotFound):
        service.transition_case(
            dsn, str(case["id"]), wrong_org, expected_version=1,
            target_status="IN_REVIEW", actor_label="local-synthetic-operator",
        )
    assert event_rows(dsn, case["id"]) == []
    # This is scope filtering, not proof of tenant authorization.


@pytest.mark.db
def test_failed_event_insert_rolls_back_status_update(test_org, monkeypatch):
    dsn, org_id = test_org
    case = new_case(test_org)

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("synthetic event persistence failure")

    monkeypatch.setattr(service, "_record_transition", fail_event)
    with pytest.raises(RuntimeError, match="synthetic event persistence failure"):
        service.transition_case(
            dsn, str(case["id"]), org_id, expected_version=1,
            target_status="IN_REVIEW", actor_label="local-synthetic-operator",
        )
    assert service.get_case(dsn, str(case["id"]), org_id)["status"] == "NEW"
    assert service.get_case(dsn, str(case["id"]), org_id)["version"] == 1
    assert event_rows(dsn, case["id"]) == []


@pytest.mark.db
def test_two_concurrent_transitions_only_one_succeeds(test_org):
    dsn, org_id = test_org
    case = new_case(test_org)
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=10)
        try:
            service.transition_case(
                dsn, str(case["id"]), org_id, expected_version=1,
                target_status="IN_REVIEW", actor_label="local-synthetic-operator",
            )
            return "success"
        except service.VersionConflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == ["stale", "success"]
    assert service.get_case(dsn, str(case["id"]), org_id)["version"] == 2
    assert len(event_rows(dsn, case["id"])) == 1


@pytest.mark.db
def test_invalid_create_input_rejected(test_org):
    dsn, org_id = test_org
    with pytest.raises(ValueError, match="title"):
        service.create_case(dsn, org_id, "  ", "Synthetic description")
    with pytest.raises(ValueError, match="description"):
        service.create_case(dsn, org_id, "Synthetic title", "")
