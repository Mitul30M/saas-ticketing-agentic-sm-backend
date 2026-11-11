"""Retrieval graph node: kb_search through the tool registry (one trace row,
one bounded timeout, validation-first)."""

from __future__ import annotations

from typing import Any

from app.agent.nodes.common import escalation_update, next_step, step_guard
from app.agent.state import AgentState
from app.config import settings
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolContext


def make_retrieve_node(registry: ToolRegistry):
    def retrieve(state: AgentState) -> dict[str, Any]:
        guard = step_guard(state)
        if guard is not None:
            return guard
        ticket_id = str(state["ticket_id"])
        trace_id = str(state["trace_id"])
        ctx = ToolContext(trace_id=trace_id, timeout_s=settings.tool_timeout_s)
        result = registry.call(
            "kb_search",
            {"query": f"{state['subject']}\n{state['body']}", "limit": settings.retrieval_top_k},
            ticket_id=ticket_id,
            ctx=ctx,
        )
        if not result.ok:
            error = result.error
            return {
                **escalation_update(
                    error.code.value if error else "TOOL_ERROR",
                    "knowledge-base retrieval failed; escalating",
                ),
                "step_count": next_step(state),
            }
        chunks = (result.data or {}).get("results") or []
        return {"retrieved": chunks, "step_count": next_step(state)}

    return retrieve
