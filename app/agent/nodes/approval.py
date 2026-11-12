"""Approval gate node: sending a reply is an ACTION and requires a recorded
human decision (invariants 1-2). The node pauses the graph with interrupt();
the resume value IS the human decision and is stored in state for the audit.

A human-edited draft is re-checked for citations — a bad edit is flagged, but
the human's authority over the final text is never overridden."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.types import interrupt

from app.agent.nodes.common import escalation_update, next_step
from app.agent.nodes.draft import (
    MAX_REPLY_CHARS,
    CitationError,
    rejects_promise_language,
    validate_citations,
)
from app.agent.state import AgentState


@dataclass(frozen=True)
class Decision:
    approved: bool
    decided_by: str
    edited_text: str | None


def parse_decision(raw: Any) -> Decision:
    """Parse the interrupt resume value; fail closed on anything malformed.

    Approval requires: dict payload, approved literally True, decided_by a
    non-blank string. edited_text counts only as a plain non-blank string
    within the draft size cap — otherwise the original draft is kept."""
    if not isinstance(raw, dict) or raw.get("approved") is not True:
        return Decision(False, "invalid_resume_payload", None)
    decided_by = raw.get("decided_by")
    if not isinstance(decided_by, str) or not decided_by.strip():
        return Decision(False, "invalid_resume_payload", None)
    edited = raw.get("edited_text")
    if isinstance(edited, str) and edited.strip() and len(edited) <= MAX_REPLY_CHARS:
        return Decision(True, decided_by.strip()[:200], edited)
    return Decision(True, decided_by.strip()[:200], None)


def make_approval_gate_node():
    def approval_gate(state: AgentState) -> dict[str, Any]:
        payload = {
            "action": (
                state.get("action")
                if state.get("action_proposed")
                else {"kind": "send_reply"}
            ),
            "ticket_id": state.get("ticket_id"),
            "draft": state.get("draft"),
            "citations": state.get("citations") or [],
            "flags": state.get("flags") or [],
        }
        decision = parse_decision(interrupt(payload))
        approved = decision.approved
        decided_by = decision.decided_by
        final_text = decision.edited_text or str(state.get("draft") or "")
        edited = decision.edited_text is not None

        flags = list(state.get("flags") or [])
        if edited:
            retrieved_ids = {str(c["kb_id"]) for c in state.get("retrieved") or []}
            try:
                validate_citations(final_text, [], retrieved_ids)
            except CitationError:
                flags.append("edited_citation_invalid")  # flagged, not blocked
            if rejects_promise_language(final_text):
                # the human is the authority; flag for the audit trail only
                flags.append("edited_promise_language")

        update: dict[str, Any] = {
            "approval": {
                "approved": approved,
                "decided_by": decided_by,
                "edited": edited,
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


def make_record_decision_node(pool: Any):
    """Record the approved outcome in state. Execution belongs to services,
    never the graph: the name says what this node actually does."""

    def record_decision(state: AgentState) -> dict[str, Any]:
        approval = state.get("approval") or {}
        if approval.get("approved"):
            return {"outcome": "action_proposed", "step_count": next_step(state)}
        return {}

    return record_decision
