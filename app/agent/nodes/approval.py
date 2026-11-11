"""Approval gate node: sending a reply is an ACTION and requires a recorded
human decision (invariants 1-2). The node pauses the graph with interrupt();
the resume value IS the human decision and is stored in state for the audit.

A human-edited draft is re-checked for citations — a bad edit is flagged, but
the human's authority over the final text is never overridden."""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.agent.nodes.common import escalation_update, next_step
from app.agent.nodes.draft import CitationError, validate_citations
from app.agent.state import AgentState


def make_approval_gate_node():
    def approval_gate(state: AgentState) -> dict[str, Any]:
        payload = {
            "action": "send_reply",
            "ticket_id": state.get("ticket_id"),
            "draft": state.get("draft"),
            "citations": state.get("citations") or [],
            "flags": state.get("flags") or [],
        }
        decision = interrupt(payload)
        if not isinstance(decision, dict):
            # Malformed resume payload: treat as rejection, never as consent.
            decision = {"approved": False, "decided_by": "invalid_resume_payload"}

        approved = bool(decision.get("approved"))
        decided_by = str(decision.get("decided_by") or "unknown")[:200]
        edited = decision.get("edited_text")
        final_text = str(edited) if isinstance(edited, str) and edited.strip() else str(
            state.get("draft") or ""
        )

        flags = list(state.get("flags") or [])
        if isinstance(edited, str) and edited.strip():
            retrieved_ids = {str(c["kb_id"]) for c in state.get("retrieved") or []}
            try:
                validate_citations(final_text, [], retrieved_ids)
            except CitationError:
                flags.append("edited_citation_invalid")  # flagged, not blocked

        update: dict[str, Any] = {
            "approval": {
                "approved": approved,
                "decided_by": decided_by,
                "edited": bool(isinstance(edited, str) and edited.strip()),
                "final_text": final_text,
            },
            "flags": flags,
            "step_count": next_step(state),
        }
        if not approved:
            update.update(
                escalation_update(
                    "DRAFT_REJECTED", f"draft rejected by {decided_by}; escalated"
                )
            )
        return update

    return approval_gate


def make_execute_node(pool: Any):
    def execute_approved(state: AgentState) -> dict[str, Any]:
        approval = state.get("approval") or {}
        if not approval.get("approved"):
            return {}
        ticket_id = str(state["ticket_id"])
        trace_id = str(state["trace_id"])
        with pool.connection() as conn:
            # Replay-safe: one recorded send per ticket; a checkpoint replay
            # must not duplicate the outbox row.
            existing = conn.execute(
                "SELECT id FROM outbox WHERE ticket_id = %s LIMIT 1", (ticket_id,)
            ).fetchone()
            if existing is None:
                row = conn.execute(
                    "INSERT INTO outbox (ticket_id, approved_by, body) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (ticket_id, str(approval.get("decided_by") or "unknown"),
                     str(approval.get("final_text") or "")),
                ).fetchone()
                outbox_id = str(row["id"]) if row else ""
            else:
                outbox_id = str(existing["id"])
        from app.agent.nodes.common import trace_node

        trace_node(
            pool, ticket_id, trace_id, "send_reply", "action",
            {"outbox_id": outbox_id}, {"sent": True, "replayed": existing is not None},
        )
        return {"outcome": "sent", "step_count": next_step(state)}

    return execute_approved
