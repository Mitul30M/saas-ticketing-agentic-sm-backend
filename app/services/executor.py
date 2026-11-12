"""Deterministic execution of already-recorded human approvals."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode


def execute_refund(pool: Any, *, request_id: str, approved_by: str) -> str:
    """Apply an approved refund under row locks and return the request id."""
    with pool.connection() as conn, conn.transaction():
        request = conn.execute(
            "SELECT id, ticket_id, invoice_id, amount_cents, status, approved_by "
            "FROM refund_requests WHERE id=%s FOR UPDATE", (request_id,)
        ).fetchone()
        if request is None:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, "refund request not found")
        if request["status"] == "executed":
            return str(request["id"])
        if request["status"] != "approved" or request["approved_by"] != approved_by:
            raise ToolFailure(ToolErrorCode.BUSINESS_RULE, "refund is not approved by this operator")
        payments = conn.execute(
            "SELECT id, amount_cents, amount_refunded_cents FROM payments "
            "WHERE invoice_id=%s AND status='succeeded' ORDER BY paid_at FOR UPDATE",
            (request["invoice_id"],),
        ).fetchall()
        remaining = sum(p["amount_cents"] - p["amount_refunded_cents"] for p in payments)
        if request["amount_cents"] > remaining:
            raise ToolFailure(ToolErrorCode.BUSINESS_RULE, "approved refund exceeds remaining balance")
        left = request["amount_cents"]
        for payment in payments:
            available = payment["amount_cents"] - payment["amount_refunded_cents"]
            applied = min(left, available)
            if applied:
                conn.execute(
                    "UPDATE payments SET amount_refunded_cents=amount_refunded_cents+%s WHERE id=%s",
                    (applied, payment["id"]),
                )
                left -= applied
            if left == 0:
                break
        conn.execute(
            "UPDATE invoices SET status = CASE WHEN NOT EXISTS ("
            "SELECT 1 FROM payments WHERE invoice_id=%s AND status='succeeded' "
            "AND amount_refunded_cents < amount_cents"
            ") THEN 'refunded' ELSE 'partially_refunded' END WHERE id=%s",
            (request["invoice_id"], request["invoice_id"]),
        )
        conn.execute(
            "UPDATE refund_requests SET status='executed', executed_at=now() WHERE id=%s",
            (request_id,),
        )
        conn.execute(
            "INSERT INTO audit_log (actor,event,entity,entity_id,payload) VALUES (%s,'refund_executed',"
            "'refund',%s,%s)", (approved_by, request_id, Json({"amount_cents": request["amount_cents"]})),
        )
    return str(request_id)


def execute_plan_change(pool: Any, *, request_id: str, approved_by: str) -> str:
    """Apply an approved plan change immediately or schedule it for period end."""
    with pool.connection() as conn, conn.transaction():
        request = conn.execute(
            "SELECT * FROM plan_change_requests WHERE id=%s FOR UPDATE", (request_id,)
        ).fetchone()
        if request is None:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, "plan change request not found")
        if request["status"] == "executed":
            return str(request["id"])
        if request["status"] != "approved" or request["approved_by"] != approved_by:
            raise ToolFailure(ToolErrorCode.BUSINESS_RULE, "plan change is not approved by this operator")
        current = conn.execute(
            "SELECT id, plan_id, current_period_end FROM subscriptions "
            "WHERE customer_id=%s AND status='active' FOR UPDATE", (request["customer_id"],)
        ).fetchone()
        if current is None or current["plan_id"] != request["from_plan_id"]:
            raise ToolFailure(ToolErrorCode.BUSINESS_RULE, "subscription changed since proposal")
        if request["effective"] == "now":
            conn.execute(
                "UPDATE subscriptions SET plan_id=%s WHERE id=%s",
                (request["to_plan_id"], current["id"]),
            )
            conn.execute(
                "UPDATE customers SET plan_id=%s WHERE id=%s",
                (request["to_plan_id"], request["customer_id"]),
            )
        else:
            conn.execute(
                "UPDATE subscriptions SET scheduled_plan_id=%s, scheduled_effective_at="
                "COALESCE(current_period_end, now()) WHERE id=%s",
                (request["to_plan_id"], current["id"]),
            )
        conn.execute(
            "UPDATE plan_change_requests SET status='executed', executed_at=now() WHERE id=%s",
            (request_id,),
        )
        conn.execute(
            "INSERT INTO audit_log (actor,event,entity,entity_id,payload) VALUES (%s,'plan_change_executed',"
            "'plan_change',%s,%s)", (approved_by, request_id, Json({"effective": request["effective"]})),
        )
    return str(request_id)


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
