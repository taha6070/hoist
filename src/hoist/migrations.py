"""Reads the Alembic revision graph from source (no app import, no DB) and flags migrations that
cannot run safely while the previous release is still serving traffic."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DeployError


@dataclass
class Revision:
    id: str
    down: tuple[str, ...]
    path: Path
    upgrade_src: ast.FunctionDef | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Finding:
    revision: str
    line: int
    message: str
    blocking: bool


def _str_or_tuple(node: ast.expr | None) -> tuple[str, ...]:
    if node is None:
        return ()
    if isinstance(node, ast.Constant):
        return (node.value,) if isinstance(node.value, str) else ()
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return ()


def load_revisions(directory: Path) -> dict[str, Revision]:
    if not directory.is_dir():
        raise DeployError(f"migrations directory not found: {directory}")
    revisions: dict[str, Revision] = {}
    for path in sorted(directory.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rev_id: str | None = None
        down: tuple[str, ...] = ()
        upgrade: ast.FunctionDef | None = None
        for node in tree.body:
            target = value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if isinstance(target, ast.Name) and target.id == "revision":
                found = _str_or_tuple(value)
                rev_id = found[0] if found else None
            elif isinstance(target, ast.Name) and target.id == "down_revision":
                down = _str_or_tuple(value)
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
                upgrade = node
        if rev_id:
            revisions[rev_id] = Revision(rev_id, down, path, upgrade)
    if not revisions:
        raise DeployError(f"no Alembic revisions found in {directory}")
    return revisions


def head_of(revisions: dict[str, Revision]) -> str:
    parents = {d for r in revisions.values() for d in r.down}
    heads = [rid for rid in revisions if rid not in parents]
    if len(heads) != 1:
        raise DeployError(f"expected exactly one Alembic head, found {len(heads)}: {', '.join(sorted(heads))}")
    return heads[0]


def _ancestors(revisions: dict[str, Revision], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        rid = stack.pop()
        if rid in seen or rid not in revisions:
            continue
        seen.add(rid)
        stack.extend(revisions[rid].down)
    return seen


def pending(revisions: dict[str, Revision], current: str | None) -> list[Revision]:
    """Revisions the server has not applied yet (empty when it is already at head)."""
    head = head_of(revisions)
    if current is None:
        todo = _ancestors(revisions, head)
    else:
        if current not in revisions:
            raise DeployError(
                f"The server database is at revision '{current}', which this checkout does not know. "
                "Is your branch behind what is deployed? Not safe to continue."
            )
        todo = _ancestors(revisions, head) - _ancestors(revisions, current)
    return sorted((revisions[r] for r in todo), key=lambda r: r.path.name)


_DESTRUCTIVE_SQL = re.compile(r"\b(drop\s+(table|column|schema)|truncate|rename\s+(to|column)|delete\s+from)\b", re.I)


def scan(revs: list[Revision]) -> list[Finding]:
    """Look only at upgrade(): downgrade() is allowed to drop things."""
    findings: list[Finding] = []
    for rev in revs:
        if rev.upgrade_src is None:
            continue
        calls = [n for n in ast.walk(rev.upgrade_src)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        # An index on a table created in this same migration has nothing to lock yet.
        new_tables = {c.args[0].value for c in calls
                      if c.func.attr == "create_table" and c.args and isinstance(c.args[0], ast.Constant)}
        for node in calls:
            name = node.func.attr
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}

            def add(msg: str, blocking: bool = True, _n=node, _r=rev) -> None:
                findings.append(Finding(_r.id, _n.lineno, msg, blocking))

            if name == "drop_table":
                add("drops a table; the previous release may still read it")
            elif name == "drop_column":
                add("drops a column; the previous release may still read it")
            elif name == "rename_table":
                add("renames a table; the previous release still uses the old name")
            elif name == "alter_column":
                if "new_column_name" in kwargs:
                    add("renames a column; the previous release still uses the old name")
                if "type_" in kwargs:
                    add("changes a column type (table rewrite / incompatible reads)")
                nullable = kwargs.get("nullable")
                if isinstance(nullable, ast.Constant) and nullable.value is False:
                    add("sets NOT NULL; the previous release may still insert NULLs")
            elif name == "execute" and node.args:
                sql = ast.unparse(node.args[0])
                if _DESTRUCTIVE_SQL.search(sql):
                    add(f"raw SQL looks destructive: {sql[:80]}")
            elif name == "create_index":
                concurrent = kwargs.get("postgresql_concurrently")
                table = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
                if table in new_tables:
                    continue
                if not (isinstance(concurrent, ast.Constant) and concurrent.value is True):
                    add("index without postgresql_concurrently=True can lock writes on a big table", blocking=False)
    return findings
