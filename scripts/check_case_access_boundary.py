"""Structural no-bypass check for protected case access (ADR-0005).

    python -m scripts.check_case_access_boundary          # exit 0 = no violations

Boundary enforced (static analysis of Python source with ``ast``):

1. **Restricted modules: every module under ``src/`` that is not listed in
   ``TRUSTED_MODULES``.** Future HTTP handlers belong in ``src/api/``, but the
   rules apply wherever a new ``src/`` module is created (fail-safe default;
   trusting a new module is a reviewed architectural change). Restricted
   modules reach case data only through the authorized layer. They:
   - may import **only**:
     - named public outcomes and the layer factory from the trusted modules in
       ``ROUTE_ALLOWED_SRC_NAMES``; never a whole trusted module, the layer
       class, the credential store, resolver, or credential-management
       functions, so a route cannot construct the layer with its own resolver
       or mint credentials;
     - other restricted ``src`` modules (which are checked by the same rules);
     - the web framework and validation libraries in ``ROUTE_ALLOWED_THIRD_PARTY``
       (minus string-import helpers and private submodules);
     - the small standard-library allowlist in ``ROUTE_ALLOWED_STDLIB``. ``sys``,
       ``os``, ``operator``, ``inspect``, ``gc``, ``importlib``, process and
       network modules are therefore excluded;
   - must not use dynamic code or reflection (``REFLECTION_CALLS`` and
     ``ROUTE_FORBIDDEN_NAMES``, for example ``getattr``, ``type``, ``open``,
     ``attrgetter``, ``ImportString``, ``get_type_hints``, including aliases
     such as ``m = __import__`` and paths such as ``logging.sys``);
   - must not access private attributes of other objects (``obj._name``; a
     class's own ``self._x`` is allowed) or reflective dunder attributes
     (``obj.__dict__``, ``__class__``, ``__getattribute__``, ``__globals__`` ...);
   - must not contain string or bytes literals naming protected tables.
2. **Everywhere else in the repository** (``scripts/``, trusted ``src/``
   modules, and any other Python file outside ``tests/``, virtual
   environments, and hidden or git-ignored output directories; those
   exclusions never apply under ``src/``):
   - web frameworks may be imported only inside ``src/api/``;
   - nothing may import the low-level, unauthenticated ``src.case_service``;
   - protected-table SQL is allowed only in ``SQL_ALLOWLIST``.
3. A missing ``src/`` directory or an unparseable file is a violation, and the
   default root is this repository, not the current working directory.

What this does NOT protect against: privileged or direct SQL clients; tests;
deliberately obfuscated code (SQL assembled at runtime, string annotations or
format strings evaluated by libraries, behavior of allowlisted third-party
packages); trusted modules misusing their access; and reviewers widening the
allowlists. Static analysis cannot make arbitrary Python safe. It complements
code review and does not replace database-level controls (row-level security
and least-privilege roles are not implemented).
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
ROUTE_PACKAGE = "src/api"
REQUIRED_ROOTS = ("src",)
# Directories never scanned: tests exercise lower-level code outside any HTTP
# boundary; the rest are environments or git-ignored local output.
EXCLUDED_TOP_DIRS = frozenset({"tests"})
EXCLUDED_DIRS = frozenset({"__pycache__", "venv", "node_modules", "build", "dist", "tmp",
                           "logs", "model-io", "pgdata", "postgres-data", "secrets"})

# Existing src modules exempt from the restricted rules. Adding one is a
# reviewed architectural change.
TRUSTED_MODULES = frozenset({
    "src/__init__.py",
    "src/case_service.py",                # local, unauthenticated case service (not route-facing)
    "src/authorized_case_operations.py",  # the authorized entry point
    "src/case_authorization.py",          # pure policy and outcomes
    "src/credential_auth.py",             # credential verification and local store
    "src/identity_provisioning.py",       # trusted local grant provisioning
    "src/model_adapter.py",               # local AI-triage model adapter
})

# The only names restricted modules may import from trusted modules.
ROUTE_ALLOWED_SRC_NAMES: dict[str, frozenset[str]] = {
    "src.authorized_case_operations": frozenset({
        "operations_from_environment", "DEFAULT_LIST_LIMIT", "MAX_LIST_LIMIT",
        "MAX_TEXT_LENGTH", "CASE_FIELDS", "LIST_FIELDS",
    }),
    "src.case_authorization": frozenset({
        "CASE_CREATE", "CASE_READ", "CASE_LIST", "CaseAccessError", "InvalidCaseRequest",
        "UnsupportedAction", "OrganizationAccessDenied", "CaseNotFound",
        "CaseOperationUnavailable", "CaseCreationOutcomeUnknown",
    }),
    "src.credential_auth": frozenset({
        "AuthenticationError", "Unauthenticated", "InactiveActor",
        "AuthenticationUnavailable", "CredentialConfigurationError",
    }),
}

ROUTE_ALLOWED_THIRD_PARTY = ("fastapi", "starlette", "pydantic", "typing_extensions")
ROUTE_FORBIDDEN_THIRD_PARTY = ("pydantic.v1",)
ROUTE_ALLOWED_STDLIB = frozenset({
    "__future__", "abc", "collections", "contextlib", "dataclasses", "datetime", "decimal",
    "enum", "functools", "json", "logging", "math", "re", "time", "typing", "uuid",
})
ROUTE_FORBIDDEN_STDLIB_SUBMODULES = ("logging.config", "logging.handlers")
# Individual names allowed from otherwise excluded standard-library modules.
ROUTE_ALLOWED_STDLIB_NAMES = frozenset({"http.HTTPStatus"})

WEB_FRAMEWORKS = ("fastapi", "starlette", "flask", "werkzeug", "django", "aiohttp",
                  "tornado", "bottle", "falcon", "sanic", "quart", "litestar",
                  "http.server", "wsgiref", "socketserver", "xmlrpc.server")

GLOBAL_FORBIDDEN_IMPORTS = ("src.case_service",)
# Built-ins flagged wherever the bare name appears (a call, or an alias such
# as ``m = __import__``).
REFLECTION_CALLS = frozenset({"__import__", "exec", "eval", "compile", "import_module",
                              "getattr", "setattr", "delattr", "vars", "globals", "locals",
                              "type", "open", "breakpoint"})
# Attribute calls flagged by name (``x.compile()`` or ``x.type()`` are ordinary).
ATTRIBUTE_REFLECTION_CALLS = frozenset({"__import__", "import_module", "exec", "eval",
                                        "getattr", "setattr", "delattr", "vars", "globals",
                                        "locals"})
REFLECTION_NAMES = frozenset({"builtins", "__builtins__"})
# Names that load code, reach attributes by string, or reach excluded modules
# through allowed ones (``logging.sys``, ``uuid.os``), wherever they appear
# (import, attribute, or bare name).
ROUTE_FORBIDDEN_NAMES = frozenset({"ImportString", "import_string", "attrgetter",
                                   "methodcaller", "getattr_static", "get_type_hints",
                                   "dictConfig", "fileConfig", "locate", "sys", "os",
                                   "modules", "codecs", "subprocess", "importlib"})
ALLOWED_DUNDER_ATTRIBUTES = frozenset({"__init__", "__name__", "__qualname__", "__doc__"})

PROTECTED_TABLES = ("case_record", "case_status_event", "ai_suggestion", "organization_membership",
                    "organization_membership_eligibility_violation")
# Whole identifiers only, so REST paths such as /case_records are not flagged.
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

_TRUSTED_DOTTED = {rel[:-3].replace("/", ".").removesuffix(".__init__"): rel
                   for rel in TRUSTED_MODULES}


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
        return [f"{base}.*" if alias.name == "*" else f"{base}.{alias.name}"
                for alias in node.names]
    return []


def _matches(name: str, prefixes) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _src_import_allowed(name: str) -> bool:
    if name in ("src", "src.*"):
        return False
    for dotted in _TRUSTED_DOTTED:
        if dotted == "src":
            continue
        if name == dotted:
            return False                       # whole trusted module
        if name.startswith(dotted + "."):
            return name[len(dotted) + 1:] in ROUTE_ALLOWED_SRC_NAMES.get(dotted, frozenset())
    return not name.endswith(".*")             # another restricted module


def _route_import_allowed(name: str) -> bool:
    parts = name.split(".")
    if any(p.startswith("_") and p != "__future__" for p in parts):
        return False                           # private modules or names
    if parts[-1] in ROUTE_FORBIDDEN_NAMES:
        return False
    if parts[0] == "src":
        return _src_import_allowed(name)
    if parts[0] == "scripts":
        return False
    if _matches(name, ROUTE_ALLOWED_THIRD_PARTY):
        return not _matches(name, ROUTE_FORBIDDEN_THIRD_PARTY)
    if name in ROUTE_ALLOWED_STDLIB_NAMES:
        return True
    return (parts[0] in ROUTE_ALLOWED_STDLIB
            and not _matches(name, ROUTE_FORBIDDEN_STDLIB_SUBMODULES))


def _strings(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        value = node.value
        yield value if isinstance(value, str) else value.decode("latin-1")
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                yield part.value


def _is_own_private_state(node: ast.Attribute) -> bool:
    """``self._x`` / ``cls._x``: a class's own single-underscore state."""
    return (isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
            and node.attr.startswith("_") and not node.attr.startswith("__"))


def _is_private(attr: str) -> bool:
    if attr.startswith("__") and attr.endswith("__"):
        return attr not in ALLOWED_DUNDER_ATTRIBUTES
    return attr.startswith("_")


def is_restricted(rel: str) -> bool:
    return rel.startswith("src/") and rel not in TRUSTED_MODULES


def _in_route_package(rel: str) -> bool:
    return rel == ROUTE_PACKAGE + ".py" or rel.startswith(ROUTE_PACKAGE + "/")


def check_file(root: Path, path: Path) -> list[Violation]:
    rel = path.relative_to(root).as_posix()
    restricted = is_restricted(rel)
    in_route_package = _in_route_package(rel)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
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
            if restricted:
                bad = [t for t in targets if not _route_import_allowed(t)]
                if bad:
                    violations.add(Violation(rel, line, "route-forbidden-import", ", ".join(bad)))
            else:
                low = [t for t in targets if _matches(t, GLOBAL_FORBIDDEN_IMPORTS)]
                if low:
                    violations.add(Violation(rel, line, "low-level-case-service-import", ", ".join(low)))
            if not in_route_package:
                web = [t for t in targets if _matches(t, WEB_FRAMEWORKS)]
                if web:
                    violations.add(Violation(rel, line, "web-framework-outside-route-package",
                                             ", ".join(web)))
        if restricted:
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in ATTRIBUTE_REFLECTION_CALLS:
                    violations.add(Violation(rel, line, "route-dynamic-code", f"{func.attr}()"))
            if isinstance(node, ast.Name):
                if node.id in REFLECTION_CALLS:  # a call or an alias of the built-in
                    violations.add(Violation(rel, line, "route-dynamic-code", f"{node.id}()"))
                elif node.id in REFLECTION_NAMES or node.id in ROUTE_FORBIDDEN_NAMES:
                    violations.add(Violation(rel, line, "route-dynamic-code", node.id))
            if isinstance(node, ast.Attribute):
                if _is_private(node.attr) and not _is_own_private_state(node):
                    violations.add(Violation(rel, line, "route-private-access", f".{node.attr}"))
                elif node.attr in ROUTE_FORBIDDEN_NAMES:
                    violations.add(Violation(rel, line, "route-dynamic-code", f".{node.attr}"))
        if rel not in SQL_ALLOWLIST or restricted:
            for text in _strings(node):
                match = _TABLE_RE.search(text)
                if match and line not in reported_sql:
                    reported_sql.add(line)
                    rule = "route-case-sql" if restricted else "protected-table-sql"
                    violations.add(Violation(rel, line, rule, f"mentions {match.group(1)}"))
    return sorted(violations, key=lambda v: (v.path, v.line, v.rule, v.detail))


def _scanned_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if parts[0] in EXCLUDED_TOP_DIRS:
            continue
        if "__pycache__" in parts:
            continue
        # Everything under src/ is scanned, whatever its directory names.
        if parts[0] != "src" and any(p in EXCLUDED_DIRS or p.startswith(".") for p in parts[:-1]):
            continue
        yield path


def check_repository(root: Path) -> list[Violation]:
    root = Path(root)
    violations: list[Violation] = [Violation(required, 0, "scanned-root-missing",
                                             f"{required}/ not found under the scanned root")
                                   for required in REQUIRED_ROOTS
                                   if not (root / required).is_dir()]
    for path in _scanned_files(root):
        violations.extend(check_file(root, path))
    return violations


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else DEFAULT_ROOT
    violations = check_repository(root)
    for violation in violations:
        print(violation)
    print(f"case-access boundary: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
