"""P3-4: static proof that the graph layer cannot execute anything.

app/agent/** must never import the approval service or executor, and its SQL
must never touch approved/executed rows, the outbox, or refund amounts. The
DB constraints (migration 006) are the hard gate; this keeps the rule true as
code grows. Reads app/agent sources only — no DB needed."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parents[2] / "app" / "agent"
BANNED_MODULES = {"app.services.executor", "app.services.approvals"}
SQL_MARKERS = ("select", "insert", "update", "delete")
BANNED_IN_SQL = ("approved", "executed", "outbox", "amount_refunded_cents")


def _sources() -> list[tuple[Path, ast.Module]]:
    parsed: list[tuple[Path, ast.Module]] = []
    for path in sorted(AGENT_DIR.rglob("*.py")):
        parsed.append((path, ast.parse(path.read_text(encoding="utf-8"))))
    assert parsed, "no agent sources found — path broken?"
    return parsed


def test_agent_never_imports_services() -> None:
    for path, tree in _sources():
        for node in ast.walk(tree):
            names: list[Any] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for module in names:
                assert module not in BANNED_MODULES, (
                    f"{path.name} imports {module}: execution belongs to services"
                )


def test_agent_sql_never_touches_side_effect_tables_or_states() -> None:
    for path, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value.lower()
            if not any(marker in text for marker in SQL_MARKERS):
                continue
            for banned in BANNED_IN_SQL:
                assert banned not in text, (
                    f"{path.name} has SQL touching '{banned}': that is the "
                    "executor's job (app/services), never the graph"
                )
