"""Deterministic execution of already-recorded human approvals."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode


def execute_approved_reply(
    pool: Any,
    *,
    ticket_id: str,
    approval_id: str,
    approved_by: str,
) -> str:
    """Write one approved reply to the simulated outbox.

    The approval row is locked and re-verified in the same transaction. A
    replay returns the existing outbox id without sending twice.

    The sent text is derived, never caller-supplied: the approval's
    edited_text when the human edited the draft, else the draft the agent
    persisted in tickets.draft_reply (the text the human saw in the gate).
    """
    with pool.connection() as conn, conn.transaction():
            approval = conn.execute(
                "SELECT id, action_type, action_ref, decision, decided_by, edited_text "
                "FROM approvals WHERE id = %s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            if approval is None:
                raise ToolFailure(ToolErrorCode.NOT_FOUND, "approval decision not found")
            if (
                approval["action_type"] != "send_reply"
                or str(approval["action_ref"]) != ticket_id
                or approval["decision"] != "approved"
                or approval["decided_by"] != approved_by
            ):
                raise ToolFailure(
                    ToolErrorCode.BUSINESS_RULE,
                    "approval does not authorize this reply",
                )
            body = approval["edited_text"]
            if body is None:
                ticket = conn.execute(
                    "SELECT draft_reply FROM tickets WHERE id = %s FOR UPDATE",
                    (ticket_id,),
                ).fetchone()
                body = ticket["draft_reply"] if ticket else None
            if not body:
                raise ToolFailure(
                    ToolErrorCode.INTERNAL, "no approved reply text available"
                )
            existing = conn.execute(
                "SELECT id FROM outbox WHERE approval_id = %s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            row = conn.execute(
                "INSERT INTO outbox (ticket_id, approved_by, body, approval_id) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (ticket_id, approved_by, body, approval_id),
            ).fetchone()
            if row is None:
                raise ToolFailure(ToolErrorCode.INTERNAL, "reply execution returned no row")
            conn.execute(
                "INSERT INTO audit_log (actor, event, entity, entity_id, payload) "
                "VALUES (%s, 'reply_executed', 'ticket', %s, %s)",
                (approved_by, ticket_id, Json({"approval_id": approval_id})),
            )
            return str(row["id"])
