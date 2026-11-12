"""Persist the graph's dashboard-facing projection with guarded transitions."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode

_TRANSITIONS = {
    "new": {"triaging"},
    "triaging": {"awaiting_approval", "awaiting_human", "failed"},
    "awaiting_approval": {"triaging", "resolved", "awaiting_human"},
    "awaiting_human": {"triaging", "resolved"},
    "failed": {"triaging"},
    "resolved": set(),
}


def _next_status(result: dict[str, Any]) -> str:
    if result.get("__interrupt__"):
        return "awaiting_approval"
    if result.get("outcome") == "escalated":
        return "awaiting_human"
    if result.get("failure"):
        return "failed"
    return "triaging"


def apply_run_result(pool: Any, ticket_id: str, result: dict[str, Any]) -> None:
    """Project a graph result; repeated identical results are harmless."""
    triage = result.get("triage") or {}
    grounding = result.get("grounding")
    escalation = result.get("escalation") or {}
    target = _next_status(result)
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT status FROM tickets WHERE id = %s FOR UPDATE", (ticket_id,)
        ).fetchone()
        if row is None:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, "ticket not found")
        current = str(row["status"])
        if current != target and target not in _TRANSITIONS.get(current, set()):
            raise ToolFailure(
                ToolErrorCode.BUSINESS_RULE,
                f"illegal ticket status transition {current} -> {target}",
            )
        conn.execute(
            "UPDATE tickets SET status = %s, triage_reasons = COALESCE(%s::jsonb, triage_reasons), "
            "kb_supported = COALESCE(%s, kb_supported), "
            "draft_citations = COALESCE(%s::text[], draft_citations), "
            "escalation_reason = COALESCE(%s, escalation_reason), thread_id = %s "
            "WHERE id = %s",
            (
                target,
                Json(triage["reasons"]) if triage.get("reasons") is not None else None,
                grounding.get("supported") if grounding else None,
                result.get("citations"),
                escalation.get("reason_code"),
                f"ticket:{ticket_id}",
                ticket_id,
            ),
        )
