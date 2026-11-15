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
            from src.authorized_case_operations import operations_from_environment
            from src.case_authorization import CaseNotFound, OrganizationAccessDenied
            from src.credential_auth import Unauthenticated
            from .schemas import CaseIn
            from . import schemas

            router = APIRouter()
            ops = operations_from_environment("postgresql:///eaiso_dev")

            def get_case(credential, case_id: UUID):
                return json.dumps(ops.get_case(credential, str(case_id)), default=str)

            class Handler(schemas.CaseIn):
                def __init__(self, **data):
                    super().__init__(**data)
        """,
        # Ordinary route code that must not be flagged (second review).
        "src/api/ordinary.py": """
            import functools
            import logging
            import re
            import time
            from http import HTTPStatus
            from typing import Annotated
            from fastapi import APIRouter, Depends, HTTPException, status

            router = APIRouter()
            logger = logging.getLogger(__name__)
            SLUG = re.compile("[a-z-]+")

            class Cache:
                def __init__(self):
                    self._items = {}
                    self._started = time.monotonic()

            @functools.lru_cache(maxsize=1)
            def settings():
                return {"case_records_path": "/case_records", "ai_suggestions": []}

            @router.get("/case_records")
            def list_route(item: Annotated[str, Depends(settings)]):
                if not SLUG.fullmatch(item):
                    raise HTTPException(status_code=HTTPStatus.BAD_REQUEST)
                return status.HTTP_200_OK
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
    # import_module(), __import__(), eval(), and the bare name importlib.
    assert found_rules.count("route-dynamic-code") == 4
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
            from scripts import provision_dev_identities

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
                 "src.identity_provisioning", "scripts.provision_dev_identities"):
        assert any(name in d for d in imports), name
    # src.helpers is not trusted, so importing it is allowed: it is itself
    # checked by the restricted rules (see the wrapper-module bypass test).
    assert not any("src.helpers" in d for d in imports)
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
    # src/ modules outside src/api/ are also restricted, so their forbidden
    # imports are reported too.
    assert rules(boundary.check_repository(root)) == [
        ("scripts/serve.py", "web-framework-outside-route-package"),
        ("src/server.py", "route-forbidden-import"),
        ("src/server.py", "web-framework-outside-route-package"),
        ("src/web/app.py", "route-forbidden-import"),
        ("src/web/app.py", "web-framework-outside-route-package"),
    ]


def test_non_route_modules_cannot_import_case_service_or_embed_case_sql(tmp_path):
    root = make_tree(tmp_path, {
        "src/__init__.py": "",
        # Trusted modules get the non-route rules.
        "src/model_adapter.py": "from src.case_service import get_case\n",
        "src/credential_auth.py": 'QUERY = "SELECT title FROM case_record"\n',
        "scripts/new_tool.py": 'SQL = "UPDATE organization_membership SET role = \'requester\'"\n',
        # Allowlisted modules may hold protected-table SQL.
        "src/authorized_case_operations.py": 'SQL = "SELECT 1 FROM case_record"\n',
        "scripts/run_triage.py": 'SQL = "INSERT INTO ai_suggestion VALUES (1)"\n',
        # Tests are outside the scanned roots.
        "tests/test_anything.py": "from src.case_service import create_case\n",
    })
    assert rules(boundary.check_repository(root)) == [
        ("scripts/new_tool.py", "protected-table-sql"),
        ("src/credential_auth.py", "protected-table-sql"),
        ("src/model_adapter.py", "low-level-case-service-import"),
    ]


def test_route_package_gets_no_sql_allowlist_exemption(tmp_path):
    root = make_tree(tmp_path, {"src/api.py": 'SQL = "DELETE FROM case_status_event"\n'})
    assert rules(boundary.check_repository(root)) == [("src/api.py", "route-case-sql")]


def test_unparseable_or_undecodable_file_is_a_violation(tmp_path):
    root = make_tree(tmp_path, {"src/api/broken.py": "def (:\n",
                                "src/api/latin1.py": b"# caf\xe9\n"})
    assert rules(boundary.check_repository(root)) == [
        ("src/api/broken.py", "unparseable"), ("src/api/latin1.py", "unparseable")]


# =============================================================================
# Readiness-review regressions: each file below is one bypass that an earlier
# version of the checker did NOT flag. Every file must now produce a violation.
# =============================================================================

BYPASSES = {
    # Identity forgery through names re-exported by allowlisted modules.
    "src/api/fake_resolver.py": """
        from uuid import UUID
        from src.credential_auth import VerifiedActor
        class R:
            def resolve(self, credential):
                return VerifiedActor(UUID(int=1), "x")
    """,
    "src/api/mint_credential.py": "from src.credential_auth import issue_credential\n",
    "src/api/custom_store.py": "from src.credential_auth import CredentialResolver, FileCredentialStore\n",
    "src/api/whole_module.py": "import src.credential_auth as c\n",
    "src/api/whole_module_from.py": "from src import credential_auth\n",
    "src/api/package_root.py": "import src\n",
    "src/api/construct_layer.py": "from src.authorized_case_operations import AuthorizedCaseOperations\n",
    "src/api/widen_policy.py": "from src.case_authorization import ROLE_PERMISSIONS\n",
    "src/api/star.py": "from src.case_authorization import *\n",
    # Private state without the obj._name syntax.
    "src/api/dunder_dict.py": "def h(ops):\n    return ops.__dict__['_dsn']\n",
    "src/api/getattribute.py": "def h(ops):\n    return object.__getattribute__(ops, '_dsn')\n",
    "src/api/attrgetter.py": "import operator\ndef h(ops):\n    return operator.attrgetter('_dsn')(ops)\n",
    "src/api/getattr_static.py": "import inspect\ndef h(ops):\n    return inspect.getattr_static(ops, '_dsn')\n",
    "src/api/globals_walk.py": "def h(ops):\n    return ops.__init__.__globals__\n",
    "src/api/gc_walk.py": "import gc\ndef h():\n    return gc.get_objects()\n",
    "src/api/type_call.py": "def h(ops, fake, dsn):\n    return type(ops)(fake, dsn)\n",
    # Standard-library paths to loaded modules, processes, or string imports.
    "src/api/sys_modules.py": "import sys\ndef h(dsn):\n    return sys.modules['psycopg'].connect(dsn)\n",
    "src/api/os_system.py": "import os\ndef h():\n    os.system('psql')\n",
    "src/api/logging_config.py": "from logging import config\n",
    "src/api/pydoc_locate.py": "import pydoc\nconnect = pydoc.locate('psycopg.connect')\n",
    "src/api/mock_patch.py": "from unittest import mock\n",
    "src/api/timeit_exec.py": "import timeit\ntimeit.timeit('import psycopg', number=1)\n",
    "src/api/asyncio_proc.py": "import asyncio\n",
    "src/api/urllib_net.py": "import urllib.request\n",
    "src/api/type_hints.py": "from typing import get_type_hints\n",
    "src/api/open_file.py": "def h():\n    return open('.local/test-credentials.json').read()\n",
    # Validation library string imports.
    "src/api/import_string.py": "from pydantic import ImportString\n",
    "src/api/pydantic_v1.py": "from pydantic.v1.utils import import_string\n",
    "src/api/private_module.py": "from pydantic._internal import _validators\n",
    # SQL in forms the old literal scan missed.
    "src/api/bytes_sql.py": "SQL = b'SELECT * FROM case_record'\n",
    "src/api/view_sql.py": "SQL = 'SELECT * FROM organization_membership_eligibility_violation'\n",
    # Route code outside src/api/, or outside src/ and scripts/ entirely.
    "src/routes/cases.py": "import psycopg\nimport os\n",
    "src/helpers/db_wrapper.py": "import psycopg\n",
    "app/main.py": "from fastapi import FastAPI\n",
    # Second review: excluded directory names inside src/, aliases, and
    # excluded modules reached through allowed ones.
    "src/build/helper.py": "import psycopg\n",
    "src/api/logs/handler.py": "from src.case_service import get_case\n",
    "src/.hidden/handler.py": "import psycopg\n",
    "src/api/alias_import.py": "m = __import__\nm('src.case_service')\n",
    "src/api/alias_getattr.py": "g = getattr\ndef h(ops):\n    return g(ops, 'x')\n",
    "src/api/logging_sys.py": "import logging\nmods = logging.sys.modules\n",
    "src/api/uuid_os.py": "import uuid\nuuid.os.system('psql')\n",
    "src/api/class_of.py": "def h(ops, fake, dsn):\n    return ops.__class__(fake, dsn)\n",
    "src/api/http_client.py": "import http.client\n",
    "server.py": "import psycopg\nSQL = 'SELECT * FROM case_record'\n",
}


def test_readiness_review_bypasses_are_each_flagged(tmp_path):
    root = make_tree(tmp_path, {"src/__init__.py": "", **BYPASSES})
    flagged = {v.path for v in boundary.check_repository(root)}
    missed = sorted(set(BYPASSES) - flagged)
    assert missed == [], f"bypasses not flagged: {missed}"
    assert boundary.main([str(root)]) == 1


def test_differently_cased_route_directory_is_still_restricted(tmp_path):
    # Separate tree: on case-insensitive file systems src/Api and src/api collide.
    root = make_tree(tmp_path, {"src/__init__.py": "", "src/Api/cases.py": "import psycopg\n"})
    assert rules(boundary.check_repository(root)) == [("src/Api/cases.py", "route-forbidden-import")]


def test_missing_scanned_root_is_a_violation(tmp_path):
    root = make_tree(tmp_path, {"scripts/tool.py": "x = 1\n"})
    assert rules(boundary.check_repository(root)) == [("src", "scanned-root-missing")]


def test_default_root_is_the_repository_not_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # an empty directory would otherwise "pass"
    assert boundary.main([]) == 0
    assert boundary.DEFAULT_ROOT == REPO


def test_utf8_bom_file_is_parsed_like_python_does(tmp_path):
    root = make_tree(tmp_path, {"src/__init__.py": "",
                                "src/api/bom.py": b"\xef\xbb\xbfimport psycopg\n",
                                "src/api/bom_ok.py": b"\xef\xbb\xbfimport uuid\n"})
    assert rules(boundary.check_repository(root)) == [("src/api/bom.py", "route-forbidden-import")]
