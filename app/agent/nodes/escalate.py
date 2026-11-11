"""Escalation + finalize nodes. Escalation goes through the escalate_to_human
TOOL (queue mapped in code from the triage category); finalize stamps the
ticket's agent_outcome/handled_by against the DB CHECK constraint."""

from __future__ import annotations

from typing import Any

from app.agent.nodes.common import trace_node
from app.agent.state import AgentState
from app.config import settings
from app.domain.enums import Queue
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolContext

_ALLOWED_DB_OUTCOMES = {"draft_ready", "action_proposed", "escalated", "failed"}


def make_escalate_node(registry: ToolRegistry):
    def escalate(state: AgentState) -> dict[str, Any]:
        ticket_id = str(state.get("ticket_id") or "")
        trace_id = str(state.get("trace_id") or "")
        esc = dict(state.get("escalation") or {})
        triage = state.get("triage") or {}
        queue = esc.get("queue") or triage.get("queue") or Queue.GENERAL.value
        reason_code = str(esc.get("reason_code") or "AGENT_ESCALATED")
        detail = str(esc.get("detail") or "escalated by agent")

        result = registry.call(
            "escalate_to_human",
            {"ticket_id": ticket_id, "reason_code": reason_code, "detail": detail},
            ticket_id=ticket_id,
            ctx=ToolContext(trace_id=trace_id, timeout_s=settings.tool_timeout_s),
        )
        errors = list(state.get("errors") or [])
        if not result.ok:
            # The human still gets the ticket (queue field below); the tool
            # failure is recorded, never swallowed.
            errors.append(
                f"escalate_to_human: {result.error.code.value if result.error else 'error'}"
            )
        return {
            "outcome": "escalated",
            "escalation": {**esc, "queue": queue},
            "errors": errors,
        }

    return escalate


def make_finalize_node(pool: Any):
    def finalize(state: AgentState) -> dict[str, Any]:
        ticket_id = str(state.get("ticket_id") or "")
        trace_id = str(state.get("trace_id") or "")
        outcome = state.get("outcome") or "failed"
        # 'sent' is the in-graph label; the DB enum has no 'sent', and an
        # approved-and-delivered reply is recorded as action_proposed.
        db_outcome = "action_proposed" if outcome == "sent" else outcome
        if db_outcome not in _ALLOWED_DB_OUTCOMES:
            db_outcome = "failed"
        with pool.connection() as conn:
            conn.execute(
                "UPDATE tickets SET agent_outcome = %s, handled_by = 'agent' "
                "WHERE id = %s",
                (db_outcome, ticket_id),
            )
        trace_node(
            pool, ticket_id, trace_id, "finalize", "node",
            {"outcome": outcome}, {"agent_outcome": db_outcome},
        )
        return {"outcome": db_outcome}

    return finalize
