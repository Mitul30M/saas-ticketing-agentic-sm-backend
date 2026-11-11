"""The Nimbus triage agent graph (PLAN 4.4, owner-narrowed scope).

The agent's ONLY tasks are: triage, RAG over the knowledge base, and (when the
grounding gates pass) drafting a reply that a human must approve. Everything
else — refunds, plan changes, sends — stays with humans and the approval
service; the agent never calls the executor (invariant 1).

Wiring: intake_validate -> injection_screen -> triage -> retrieve ->
grounding_check -> draft_reply -> approval_gate (interrupt) -> execute_approved
-> finalize; any failure/abstention routes to escalate -> finalize.

Bounded execution: explicit step_count guard in every LLM/tool node
(settings.step_limit) plus a recursion_limit passed at invoke time (run.py)."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.approval import make_approval_gate_node, make_execute_node
from app.agent.nodes.common import safe_node
from app.agent.nodes.draft import make_draft_node
from app.agent.nodes.escalate import make_escalate_node, make_finalize_node
from app.agent.nodes.grounding import make_grounding_node
from app.agent.nodes.intake import make_intake_node
from app.agent.nodes.retrieve import make_retrieve_node
from app.agent.nodes.screen import make_screen_node
from app.agent.nodes.triage_node import make_triage_node
from app.agent.state import AgentState
from app.llm.base import LLMClient
from app.tools.registry import ToolRegistry, build_registry


def _routed(next_node: str):
    """Router: an escalated outcome diverts to the escalate node."""
    return lambda state: (
        "escalate" if state.get("outcome") == "escalated" else next_node
    )


def _after_approval(state: AgentState) -> str:
    approval = state.get("approval") or {}
    return "execute_approved" if approval.get("approved") else "escalate"


def build_graph(
    pool: Any,
    client: LLMClient | None = None,
    registry: ToolRegistry | None = None,
    checkpointer: Any | None = None,
):
    """Assemble the graph. pool/client/registry may be None for diagram
    generation; every node is wrapped in safe_node so unexpected failures end
    in a fail-closed escalation."""
    registry = registry or build_registry(pool)
    client = client or LLMClient(pool=pool)

    builder = StateGraph(AgentState)
    builder.add_node("intake_validate",
                     safe_node("intake_validate", pool, make_intake_node(pool)))
    builder.add_node("injection_screen",
                     safe_node("injection_screen", pool, make_screen_node(pool)))
    builder.add_node("triage", safe_node("triage", pool, make_triage_node(pool, client)))
    builder.add_node("retrieve", safe_node("retrieve", pool, make_retrieve_node(registry)))
    builder.add_node("grounding_check",
                     safe_node("grounding_check", pool, make_grounding_node(pool, client)))
    builder.add_node("draft_reply",
                     safe_node("draft_reply", pool, make_draft_node(pool, client)))
    builder.add_node("approval_gate",
                     safe_node("approval_gate", pool, make_approval_gate_node()))
    builder.add_node("execute_approved",
                     safe_node("execute_approved", pool, make_execute_node(pool)))
    builder.add_node("escalate", safe_node("escalate", pool, make_escalate_node(registry)))
    builder.add_node("finalize", safe_node("finalize", pool, make_finalize_node(pool)))

    builder.add_edge(START, "intake_validate")
    builder.add_conditional_edges(
        "intake_validate", _routed("injection_screen"), ["injection_screen", "escalate"])
    builder.add_conditional_edges(
        "injection_screen", _routed("triage"), ["triage", "escalate"])
    builder.add_conditional_edges(
        "triage", _routed("retrieve"), ["retrieve", "escalate"])
    builder.add_conditional_edges(
        "retrieve", _routed("grounding_check"), ["grounding_check", "escalate"])
    builder.add_conditional_edges(
        "grounding_check", _routed("draft_reply"), ["draft_reply", "escalate"])
    builder.add_conditional_edges(
        "draft_reply", _routed("approval_gate"), ["approval_gate", "escalate"])
    builder.add_conditional_edges(
        "approval_gate", _after_approval, ["execute_approved", "escalate"])
    builder.add_edge("execute_approved", "finalize")
    builder.add_edge("escalate", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)
