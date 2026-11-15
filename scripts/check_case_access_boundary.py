"""Structural no-bypass check for protected case access (ADR-0005).

    python -m scripts.check_case_access_boundary          # exit 0 = no violations

Boundary enforced (static analysis of Python source with ``ast``):

1. **Route package** (``src/api/``, where future HTTP handlers must live).
   Route modules may reach case data only through
   ``src.authorized_case_operations``. They:
   - may import **only** allowlisted modules: the authorized layer, the policy
     outcomes, the credential module, other ``src.api`` modules, the web
     framework and validation libraries listed below, and the standard library
     minus modules that enable dynamic code, databases, or processes;
   - must not use dynamic code or reflection (``__import__``, ``exec``,
     ``eval``, ``compile``, ``import_module``, ``getattr``/``setattr``/
     ``delattr``, ``vars``, ``globals``, ``locals``, ``builtins``);
   - must not access private attributes (``obj._name``), for example the
     authorized layer's internal connection or DSN;
   - must not contain string literals naming protected tables (case SQL).
2. **Everywhere else under ``src/`` and ``scripts/``.**
   - Web frameworks may be imported only inside the route package, so route
     handlers cannot live outside the checked package.
   - Nothing may import the low-level, unauthenticated ``src.case_service``.
     Tests are exempt: they exercise lower-level code outside any HTTP boundary.
   - String literals naming protected tables are allowed only in the modules
     listed in ``SQL_ALLOWLIST`` (the existing local services and scripts).

What this does NOT protect against: privileged or direct SQL clients; code
that runs outside ``src/`` and ``scripts/``; tests; obfuscated access (SQL
assembled at runtime, views or functions with other names, data files);
allowlisted modules misusing their access; and reviewers widening the
allowlists. It complements code review and does not replace database-level
controls (row-level security and least-privilege roles are not implemented).
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROUTE_PACKAGES = ("src/api",)
SCANNED_ROOTS = ("src", "scripts")

WEB_FRAMEWORKS = ("fastapi", "starlette", "flask", "werkzeug", "django", "aiohttp",
                  "tornado", "bottle", "falcon", "sanic", "quart", "litestar",
                  "http.server", "wsgiref", "socketserver", "xmlrpc.server")

# Route modules may import only these (prefix match on dotted names) ...
ROUTE_ALLOWED_IMPORTS = (
    "src.authorized_case_operations", "src.case_authorization", "src.credential_auth", "src.api",
    "fastapi", "starlette", "pydantic", "typing_extensions",
)
# ... plus the standard library, except modules that enable dynamic code,
# database access, serialization of arbitrary objects, or subprocesses.
ROUTE_FORBIDDEN_STDLIB = frozenset({
    "importlib", "runpy", "pkgutil", "zipimport", "ctypes", "builtins", "code", "codeop",
    "sqlite3", "dbm", "shelve", "pickle", "marshal", "subprocess", "multiprocessing",
    "socket", "http", "wsgiref", "socketserver", "xmlrpc",
})
STDLIB = frozenset(sys.stdlib_module_names)

GLOBAL_FORBIDDEN_IMPORTS = ("src.case_service",)
REFLECTION_CALLS = frozenset({"__import__", "exec", "eval", "compile", "import_module",
                              "getattr", "setattr", "delattr", "vars", "globals", "locals"})
REFLECTION_NAMES = frozenset({"builtins", "__builtins__"})

PROTECTED_TABLES = ("case_record", "case_status_event", "ai_suggestion", "organization_membership")
_TABLE_RE = re.compile(r"\b(" + "|".join(PROTECTED_TABLES) + r")\b", re.IGNORECASE)

# Existing modules that legitimately hold protected-table SQL. Adding a module
# here is a reviewed architectural change.
SQL_ALLOWLIST = frozenset({
    "src/case_service.py",              # local, unauthenticated case service (not route-facing)
    "src/authorized_case_operations.py",  # the authorized entry point
    "src/identity_provisioning.py",     # trusted local grant provisioning
    "scripts/run_triage.py",            # local AI-triage experiment
    "scripts/check_case_access_boundary.py",  # this checker's own table list
})


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


def _module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_targets(node: ast.AST, module: str, is_package: bool) -> list[str]:
    """Fully qualified targets of an import statement ("from a import b" -> "a.b")."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            pkg = module.split(".") if is_package else module.split(".")[:-1]
            if node.level > 1:
                pkg = pkg[: len(pkg) - (node.level - 1)]
            base = ".".join(pkg + ([node.module] if node.module else []))
        return [base if alias.name == "*" else f"{base}.{alias.name}" for alias in node.names]
    return []


def _matches(name: str, prefixes) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _route_import_allowed(name: str) -> bool:
    if _matches(name, ROUTE_ALLOWED_IMPORTS):
        return True
    top = name.split(".")[0]
    return top in STDLIB and top not in ROUTE_FORBIDDEN_STDLIB and top not in ("src", "scripts")


def _strings(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                yield part.value


def _is_private(attr: str) -> bool:
    return attr.startswith("_") and not (attr.startswith("__") and attr.endswith("__"))


def check_file(root: Path, path: Path) -> list[Violation]:
    rel = path.relative_to(root).as_posix()
    in_route = any(rel == p + ".py" or rel.startswith(p + "/") for p in ROUTE_PACKAGES)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
        line = getattr(exc, "lineno", 0) or 0
        return [Violation(rel, line, "unparseable", "file cannot be read or parsed")]
    module = _module_name(rel)
    is_package = path.name == "__init__.py"
    violations: set[Violation] = set()
    reported_sql: set[int] = set()

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        targets = _import_targets(node, module, is_package)
        if targets:
            if in_route:
                bad = [t for t in targets if not _route_import_allowed(t)]
                if bad:
                    violations.add(Violation(rel, line, "route-forbidden-import", ", ".join(bad)))
            else:
                low = [t for t in targets if _matches(t, GLOBAL_FORBIDDEN_IMPORTS)]
                if low:
                    violations.add(Violation(rel, line, "low-level-case-service-import", ", ".join(low)))
                web = [t for t in targets if _matches(t, WEB_FRAMEWORKS)]
                if web:
                    violations.add(Violation(rel, line, "web-framework-outside-route-package",
                                             ", ".join(web)))
        if in_route:
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else None)
                if called in REFLECTION_CALLS:
                    violations.add(Violation(rel, line, "route-dynamic-code", f"{called}()"))
            if isinstance(node, ast.Name) and node.id in REFLECTION_NAMES:
                violations.add(Violation(rel, line, "route-dynamic-code", node.id))
            if isinstance(node, ast.Attribute) and _is_private(node.attr):
                violations.add(Violation(rel, line, "route-private-access", f".{node.attr}"))
        if rel not in SQL_ALLOWLIST or in_route:
            for text in _strings(node):
                match = _TABLE_RE.search(text)
                if match and line not in reported_sql:
                    reported_sql.add(line)
                    rule = "route-case-sql" if in_route else "protected-table-sql"
                    violations.add(Violation(rel, line, rule, f"mentions {match.group(1)}"))
    return sorted(violations, key=lambda v: (v.path, v.line, v.rule, v.detail))


def check_repository(root: Path) -> list[Violation]:
    root = Path(root)
    violations: list[Violation] = []
    for scanned in SCANNED_ROOTS:
        base = root / scanned
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            violations.extend(check_file(root, path))
    return violations


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path(".")
    violations = check_repository(root)
    for violation in violations:
        print(violation)
    print(f"case-access boundary: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
