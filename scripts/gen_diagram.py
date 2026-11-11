"""Regenerate docs/architecture.md with the current graph's mermaid diagram.

    PYTHONPATH=. uv run python scripts/gen_diagram.py
"""

from __future__ import annotations

from pathlib import Path

from app.agent.graph import build_graph

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "architecture.md"

graph = build_graph(pool=None)  # no calls are made; nodes stay lazy
mermaid = graph.get_graph().draw_mermaid()

OUT.write_text(
    "# Architecture — agent graph\n\n"
    "Regenerate with `uv run python scripts/gen_diagram.py` "
    "(keep in sync with `app/agent/graph.py`).\n\n"
    f"```mermaid\n{mermaid}```\n",
    encoding="utf-8",
)
print(f"wrote {OUT}")
