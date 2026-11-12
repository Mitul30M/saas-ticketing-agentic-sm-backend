"""Persist the graph's dashboard-facing projection with guarded transitions.

The status machine lives here (not only in SQL) so an illegal regression is a
typed error instead of a silent write. The approval/execution state machines
(refund/plan-change rows) are separate and remain database-enforced.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode

# Forward-only. `resolved -> triaging` exists ONLY for an explicit operator
# re-run (`POST /api/tickets/{id}/run`), which never executes an action by
# itself: a re-run just produces a new draft that must be approved again.
_TRANSITIONS: dict[str, set[str]] = {
    "new": {"triaging"},
    "triaging": {"awaiting_approval", "awaiting_human", "resolved", "failed"},
    "awaiting_approval": {"triaging", "resolved", "awaiting_human"},
    "awaiting_human": {"triaging", "resolved"},
    "failed": {"triaging"},
    "resolved": {"triaging"},
}


def _target_status(result: dict[str, Any]) -> str:
    """Map a finished graph result to the ticket status a human should see.

    A run that ended in a failure or an abstention is ALWAYS parked for a human
    (`awaiting_human`) — the safe state — never dropped or left `triaging`.
    """
    if result.get("__interrupt__"):
        return "awaiting_approval"
    if result.get("outcome") == "escalated" or result.get("failure"):
        return "awaiting_human"
    return "triaging"


def _agent_outcome(result: dict[str, Any]) -> str:
    if result.get("__interrupt__"):
        return "action_proposed"
    if result.get("failure"):
        return "failed"
    if result.get("outcome") == "escalated":
        return "escalated"
    if (result.get("approval") or {}).get("approved"):
        return "action_proposed"
    return "draft_ready"


def _check_transition(current: str, target: str) -> None:
    if current != target and target not in _TRANSITIONS.get(current, set()):
        raise ToolFailure(
            ToolErrorCode.BUSINESS_RULE,
            f"illegal ticket status transition {current} -> {target}",
        )


def transition_ticket(
    conn: Any, ticket_id: str, target: str, *, thread_id: str | None = None
) -> None:
    """Lock the ticket row and move it through the guarded state machine.

    Used by services that already hold a connection/transaction (e.g. the
    re-run enqueue). Raises ToolFailure on an illegal transition.
    """
    row = conn.execute(
        "SELECT status FROM tickets WHERE id = %s FOR UPDATE", (ticket_id,)
    ).fetchone()
    if row is None:
        raise ToolFailure(ToolErrorCode.NOT_FOUND, "ticket not found")
    _check_transition(str(row["status"]), target)
    if thread_id is not None:
        conn.execute(
            "UPDATE tickets SET status=%s, thread_id=%s WHERE id=%s",
            (target, thread_id, ticket_id),
        )
    else:
        conn.execute("UPDATE tickets SET status=%s WHERE id=%s", (target, ticket_id))


def apply_run_result(pool: Any, ticket_id: str, result: dict[str, Any]) -> None:
    """Project a graph result; repeated identical results are harmless."""
    triage = result.get("triage") or {}
    grounding = result.get("grounding")
    escalation = result.get("escalation") or {}
    target = _target_status(result)
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT status FROM tickets WHERE id = %s FOR UPDATE", (ticket_id,)
        ).fetchone()
        if row is None:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, "ticket not found")
        _check_transition(str(row["status"]), target)
        conn.execute(
            "UPDATE tickets SET status = %s, agent_outcome = %s, "
            "handled_by = CASE WHEN %s = 'awaiting_approval' THEN 'agent' ELSE handled_by END, "
            "triage_reasons = COALESCE(%s::jsonb, triage_reasons), "
            "kb_supported = COALESCE(%s, kb_supported), "
            "draft_citations = COALESCE(%s::text[], draft_citations), "
            "flags = COALESCE(%s::text[], flags), "
            "escalation_reason = COALESCE(%s, escalation_reason), thread_id = %s "
            "WHERE id = %s",
            (
                target,
                _agent_outcome(result),
                target,
                Json(triage["reasons"]) if triage.get("reasons") is not None else None,
                grounding.get("supported") if grounding else None,
                result.get("citations"),
                result.get("flags"),
                escalation.get("reason_code"),
                result.get("thread_id") or f"ticket:{ticket_id}",
                ticket_id,
            ),
        )


def mark_resolved(pool: Any, ticket_id: str) -> None:
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT status FROM tickets WHERE id=%s FOR UPDATE", (ticket_id,)
        ).fetchone()
        if row is None:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, "ticket not found")
        current = str(row["status"])
        if current == "resolved":
            return
        _check_transition(current, "resolved")
        conn.execute(
            "UPDATE tickets SET status='resolved', agent_outcome='action_proposed', "
            "handled_by='agent' WHERE id=%s",
            (ticket_id,),
        )
