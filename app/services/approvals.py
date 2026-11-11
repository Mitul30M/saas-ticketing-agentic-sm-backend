"""Human decision recording and approval-service orchestration."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode


def record_reply_decision(
    pool: Any,
    *,
    ticket_id: str,
    decided_by: str,
    decision: str,
    edited_text: str | None = None,
    note: str | None = None,
) -> str:
    """Record one final human decision for a reply.

    This function records the decision only. The executor is a separate,
    explicit call so API/job code can resume the graph before executing.
    """
    if decision not in {"approved", "rejected"}:
        raise ToolFailure(ToolErrorCode.INVALID_INPUT, "decision must be approved or rejected")
    if not decided_by.strip():
        raise ToolFailure(ToolErrorCode.INVALID_INPUT, "decided_by is required")
    with pool.connection() as conn, conn.transaction():
            existing = conn.execute(
                "SELECT id, decision FROM approvals "
                "WHERE action_type = 'send_reply' AND action_ref = %s "
                "AND decision IN ('approved', 'rejected') FOR UPDATE",
                (ticket_id,),
            ).fetchone()
            if existing is not None:
                if existing["decision"] != decision:
                    raise ToolFailure(
                        ToolErrorCode.DUPLICATE,
                        "a final decision already exists for this reply",
                    )
                return str(existing["id"])
            ticket = conn.execute(
                "SELECT id, draft_reply FROM tickets WHERE id = %s FOR UPDATE",
                (ticket_id,),
            ).fetchone()
            if ticket is None:
                raise ToolFailure(ToolErrorCode.NOT_FOUND, "ticket not found")
            row = conn.execute(
                "INSERT INTO approvals "
                "(ticket_id, action_type, action_ref, decision, edited_text, decided_by, note) "
                "VALUES (%s, 'send_reply', %s, %s, %s, %s, %s) RETURNING id",
                (ticket_id, ticket_id, decision, edited_text, decided_by[:200], note),
            ).fetchone()
            if row is None:
                raise ToolFailure(ToolErrorCode.INTERNAL, "approval decision returned no row")
            conn.execute(
                "INSERT INTO audit_log (actor, event, entity, entity_id, payload) "
                "VALUES (%s, 'reply_decision_recorded', 'ticket', %s, %s)",
                (decided_by[:200], ticket_id, Json({"decision": decision})),
            )
            return str(row["id"])
