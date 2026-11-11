"""Triage graph node: wraps triage_ticket (LLM proposes; code disposes — the
queue comes from CATEGORY_QUEUE in code) and persists the triage verdict."""

from __future__ import annotations

from typing import Any

from app.agent.nodes.common import next_step, step_guard, trace_node
from app.agent.nodes.triage import triage_ticket
from app.agent.resilience import call_llm
from app.agent.state import AgentState
from app.llm.base import LLMClient


def make_triage_node(pool: Any, client: LLMClient):
    def triage_node(state: AgentState) -> dict[str, Any]:
        guard = step_guard(state)
        if guard is not None:
            return guard
        ticket_id = str(state["ticket_id"])
        trace_id = str(state["trace_id"])
        result = call_llm(
            lambda: triage_ticket(client, state["subject"], state["body"]),
            pool=pool, ticket_id=ticket_id, trace_id=trace_id, node="triage",
        )
        with pool.connection() as conn:
            conn.execute(
                "UPDATE tickets SET category = %s, priority = %s, sentiment = %s, "
                "queue = %s WHERE id = %s",
                (result.category.value, result.priority.value, result.sentiment.value,
                 result.queue.value, ticket_id),
            )
        trace_node(
            pool, ticket_id, trace_id, "triage", "llm",
            {"prompt_version": result.prompt_version},
            {"category": result.category.value, "priority": result.priority.value,
             "sentiment": result.sentiment.value, "queue": result.queue.value,
             "reasons": result.reasons},
            duration_ms=result.latency_ms,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
            cache_hit=result.cache_hit,
        )
        return {"triage": result.to_dict(), "step_count": next_step(state)}

    return triage_node
