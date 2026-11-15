"""Structural no-bypass check (scripts/check_case_access_boundary.py). Offline."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from scripts import check_case_access_boundary as boundary

REPO = Path(__file__).resolve().parents[1]


def make_tree(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(dedent(text), encoding="utf-8")
    return tmp_path


def rules(violations):
    return sorted((v.path, v.rule) for v in violations)


def test_repository_respects_the_boundary():
    assert boundary.check_repository(REPO) == []
    assert boundary.main([str(REPO)]) == 0


def test_permitted_route_module_passes(tmp_path):
    root = make_tree(tmp_path, {
        "src/__init__.py": "",
        "src/api/__init__.py": "",
        "src/api/schemas.py": "from pydantic import BaseModel\nclass CaseIn(BaseModel):\n    title: str\n",
        "src/api/cases.py": """
            import json
            from uuid import UUID
            from fastapi import APIRouter
            from src.authorized_case_operations import AuthorizedCaseOperations
            from src.case_authorization import CaseNotFound, OrganizationAccessDenied
            from src import credential_auth
            from .schemas import CaseIn

            router = APIRouter()

            def get_case(ops: AuthorizedCaseOperations, credential, case_id: UUID):
                return json.dumps(ops.get_case(credential, str(case_id)), default=str)
        """,
    })
    assert boundary.check_repository(root) == []


def test_forbidden_route_module_fails_on_every_bypass(tmp_path):
    root = make_tree(tmp_path, {
        "src/api/bad_routes.py": """
            import psycopg
            import src.case_service
            from src import case_service as svc
            from src.case_service import transition_case
            from src.identity_provisioning import grant_access
            import importlib

            def handler(dsn, case_id):
                importlib.import_module("src.case_service")
                __import__("src.case_service")
                eval("1")
                with psycopg.connect(dsn) as c:
                    return c.execute("SELECT * FROM case_record WHERE id = %s", (case_id,))

            def lister(c, org):
                return c.execute(f"SELECT id FROM Case_Record WHERE organization_id = {org}")
        """,
    })
    found = boundary.check_repository(root)
    found_rules = [v.rule for v in found]
    assert found_rules.count("route-forbidden-import") == 6
    assert found_rules.count("route-dynamic-code") == 3
    assert found_rules.count("route-case-sql") == 2
    details = " ".join(v.detail for v in found)
    for name in ("psycopg", "src.case_service", "src.case_service.transition_case",
                 "src.identity_provisioning.grant_access", "importlib"):
        assert name in details
    assert boundary.main([str(root)]) == 1


def test_route_imports_are_allowlist_only_and_reflection_is_blocked(tmp_path):
    root = make_tree(tmp_path, {
        "src/api/sneaky.py": """
            import psycopg_pool
            import pg8000
            import runpy
            import sqlite3
            import builtins
            from src import identity_provisioning
            from src.helpers import db_wrapper

            def handler(ops, credential):
                conn = ops._connect(autocommit=True)
                dsn = ops._dsn
                loader = getattr(builtins, "__import__")
                return vars(ops), conn, dsn, loader, __builtins__
        """,
    })
    found = boundary.check_repository(root)
    imports = [v.detail for v in found if v.rule == "route-forbidden-import"]
    assert len(imports) == 7
    for name in ("psycopg_pool", "pg8000", "runpy", "sqlite3", "builtins",
                 "src.identity_provisioning", "src.helpers.db_wrapper"):
        assert any(name in d for d in imports), name
    assert {v.detail for v in found if v.rule == "route-private-access"} == {"._connect", "._dsn"}
    dynamic = {v.detail for v in found if v.rule == "route-dynamic-code"}
    assert {"getattr()", "vars()", "builtins", "__builtins__"} <= dynamic


def test_relative_import_of_case_service_from_route_package_is_caught(tmp_path):
    root = make_tree(tmp_path, {
        "src/api/routes/cases.py": "from ... import case_service\nfrom ...case_service import get_case\n",
    })
    found = boundary.check_repository(root)
    assert [v.rule for v in found] == ["route-forbidden-import", "route-forbidden-import"]
    assert all("src.case_service" in v.detail for v in found)


def test_web_framework_outside_route_package_is_caught(tmp_path):
    root = make_tree(tmp_path, {
        "src/web/app.py": "from fastapi import FastAPI\nimport psycopg\n",
        "src/server.py": "import http.server\n",
        "scripts/serve.py": "from flask import Flask\n",
    })
    assert rules(boundary.check_repository(root)) == [
        ("scripts/serve.py", "web-framework-outside-route-package"),
        ("src/server.py", "web-framework-outside-route-package"),
        ("src/web/app.py", "web-framework-outside-route-package"),
    ]


def test_non_route_modules_cannot_import_case_service_or_embed_case_sql(tmp_path):
    root = make_tree(tmp_path, {
        "src/reporting.py": "from src.case_service import get_case\n",
        "src/exporter.py": 'QUERY = "SELECT title FROM case_record"\n',
        "scripts/new_tool.py": 'SQL = "UPDATE organization_membership SET role = \'requester\'"\n',
        # Allowlisted modules may hold protected-table SQL.
        "src/authorized_case_operations.py": 'SQL = "SELECT 1 FROM case_record"\n',
        "scripts/run_triage.py": 'SQL = "INSERT INTO ai_suggestion VALUES (1)"\n',
        # Tests are outside the scanned roots.
        "tests/test_anything.py": "from src.case_service import create_case\n",
    })
    assert rules(boundary.check_repository(root)) == [
        ("scripts/new_tool.py", "protected-table-sql"),
        ("src/exporter.py", "protected-table-sql"),
        ("src/reporting.py", "low-level-case-service-import"),
    ]


def test_route_package_gets_no_sql_allowlist_exemption(tmp_path):
    root = make_tree(tmp_path, {"src/api.py": 'SQL = "DELETE FROM case_status_event"\n'})
    assert rules(boundary.check_repository(root)) == [("src/api.py", "route-case-sql")]


def test_unparseable_or_undecodable_file_is_a_violation(tmp_path):
    root = make_tree(tmp_path, {"src/api/broken.py": "def (:\n",
                                "src/api/latin1.py": b"# caf\xe9\n"})
    assert rules(boundary.check_repository(root)) == [
        ("src/api/broken.py", "unparseable"), ("src/api/latin1.py", "unparseable")]
