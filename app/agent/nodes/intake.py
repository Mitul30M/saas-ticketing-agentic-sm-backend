"""Intake validation node: the ticket row must exist and carry the transport
sender (from_email, invariant 11) plus a non-empty body."""

from __future__ import annotations

import uuid
from typing import Any

from app.agent.nodes.common import escalation_update, next_step
from app.agent.state import AgentState


def make_intake_node(pool: Any):
    def intake(state: AgentState) -> dict[str, Any]:
        trace_id = state.get("trace_id") or uuid.uuid4().hex
        ticket_id = str(state.get("ticket_id") or "")
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT id, subject, body, from_email FROM tickets WHERE id = %s",
                (ticket_id,),
            ).fetchone()
        if row is None:
            # Unknown ticket: nothing to write on tickets; trace_events.ticket_id
            # is TEXT so the audit row still records the attempt.
            return {
                "trace_id": trace_id,
                **escalation_update("TICKET_NOT_FOUND", f"no ticket row for id {ticket_id}"),
            }
        subject = str(row["subject"] or "")
        body = str(row["body"] or "")
        from_email = str(row["from_email"] or "")
        if not body.strip() or not from_email.strip():
            return {
                "trace_id": trace_id,
                **escalation_update(
                    "BAD_TICKET", "ticket is missing body text or a transport sender"
                ),
            }
        return {
            "trace_id": trace_id,
            "ticket_id": str(row["id"]),
            "subject": subject,
            "body": body,
            "step_count": next_step(state),
        }

    return intake
