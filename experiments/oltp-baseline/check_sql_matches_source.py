"""Check that the measured SQL is the application's SQL, not a rewrite.

Extracts the statement literals passed to ``conn.execute`` in
``AuthorizedCaseOperations.get_case`` and ``list_cases`` from
``src/authorized_case_operations.py`` (by parsing the source; nothing is
imported or executed), converts psycopg placeholders to positional ones in
order of first appearance, and compares each with the ``PREPARE ... AS`` body
of the matching experiment file. Only whitespace is normalized.

Scope: this compares statement text only. It does not check the PREPARE
parameter types or the EXECUTE values; those are documented in each SQL file
and confirmed separately by the auto_explain capture of the real calls. It
selects statements by their order among constant-string ``execute`` calls, so
a source change fails as DIFFERS (or an error), not as a false MATCH.

Run from the repository root:  python experiments/oltp-baseline/check_sql_matches_source.py
Exit 0 when all three statements match; 1 otherwise.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1] / "src" / "authorized_case_operations.py"

# (method, index of the conn.execute call within it, experiment file)
EXPECTED = [
    ("get_case", 0, "sql/query_a0_actor_status.sql"),
    ("get_case", 1, "sql/query_a_get_case.sql"),
    ("list_cases", 0, "sql/query_b_list_cases.sql"),
]


def _execute_literals(tree: ast.AST, method: str) -> list[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            calls = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "execute" and n.args
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
            ]
            calls.sort(key=lambda n: (n.lineno, n.col_offset))
            return [c.args[0].value for c in calls]
    raise SystemExit(f"method {method} not found in {SOURCE}")


def _positional(sql: str) -> str:
    order: dict[str, int] = {}

    def named(m: re.Match) -> str:
        order.setdefault(m.group(1), len(order) + 1)
        return f"${order[m.group(1)]}"

    sql = re.sub(r"%\((\w+)\)s", named, sql)
    count = iter(range(1, 100))
    return re.sub(r"%s", lambda _m: f"${next(count)}", sql)


def _prepared_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^PREPARE\s+\w+\s*\([^)]*\)\s+AS\s*\n(.*?);\s*$", text, re.S | re.M)
    if m is None:
        raise SystemExit(f"no PREPARE ... AS body in {path}")
    return m.group(1)


def _norm(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";").strip()


def main() -> int:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    ok = True
    for method, index, rel in EXPECTED:
        literals = _execute_literals(tree, method)
        source_sql = _norm(_positional(literals[index]))
        experiment_sql = _norm(_prepared_body(HERE / rel))
        match = source_sql == experiment_sql
        ok &= match
        print(f"{'MATCH' if match else 'DIFFERS'}  {method}() execute #{index + 1}  ->  {rel}")
        if not match:
            print(f"  source:     {source_sql}\n  experiment: {experiment_sql}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
