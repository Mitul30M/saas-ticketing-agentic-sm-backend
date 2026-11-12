"""Deterministic planning and narrow, validated action proposals."""

from __future__ import annotations

import re
from typing import Any

from app.agent.state import AgentState
from app.config import settings
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolContext

INVOICE_RE = re.compile(r"\bINV-\d{4,8}-[A-Z0-9]+\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
PLAN_WORD_RE = re.compile(r"\b(free|starter|pro|business)\b", re.IGNORECASE)
_FROM_TO_RE = re.compile(
    r"\bfrom\s+(free|starter|pro|business)\s+to\s+(free|starter|pro|business)\b",
    re.IGNORECASE,
)
_TO_FROM_RE = re.compile(
    r"\bto\s+(free|starter|pro|business)\s+from\s+(free|starter|pro|business)\b",
    re.IGNORECASE,
)
PLAN_IDS = {
    "free": "PLAN-FREE",
    "starter": "PLAN-STARTER",
    "pro": "PLAN-PRO",
    "business": "PLAN-BUSINESS",
}


def parse_amount_cents(text: str) -> int:
    """The refund amount the customer asked for, in cents.

    Accepts thousands separators. Several DISTINCT amounts in one ticket are
    ambiguous -> ValueError (escalated, never guessed). Repeating the same
    amount is fine."""
    values = {
        round(float(match.group(1).replace(",", "")) * 100)
        for match in AMOUNT_RE.finditer(text)
    }
    if not values or len(values) > 1:
        raise ValueError("no single unambiguous dollar amount in the ticket")
    return values.pop()


def parse_target_plan(text: str) -> str:
    """The plan the customer asked to move TO.

    One mentioned plan decides; with several, only a directional from/to
    reading decides, and only when it accounts for every mentioned plan —
    "from Pro to Starter, not Business" stays ambiguous."""
    mentioned = {match.group(1).lower() for match in PLAN_WORD_RE.finditer(text)}
    if len(mentioned) == 1:
        return PLAN_IDS[mentioned.pop()]
    to_from = _TO_FROM_RE.search(text)
    if to_from and mentioned <= {to_from.group(1).lower(), to_from.group(2).lower()}:
        return PLAN_IDS[to_from.group(1).lower()]
    from_to = _FROM_TO_RE.search(text)
    if from_to and mentioned <= {from_to.group(1).lower(), from_to.group(2).lower()}:
        return PLAN_IDS[from_to.group(2).lower()]
    raise ValueError("no unambiguous target plan in the ticket")


def make_plan_node():
    def plan(state: AgentState) -> dict[str, Any]:
        category = (state.get("triage") or {}).get("category")
        if category == "refund_request":
            action = "refund"
        elif category == "plan_change":
            action = "plan_change"
        else:
            action = "reply"
        return {"plan": action}

    return plan


def make_tool_executor_node(registry: ToolRegistry):
    def tool_executor(state: AgentState) -> dict[str, Any]:
        action = state.get("plan")
        if action not in {"refund", "plan_change"}:
            return {"action_context": {"kind": "reply"}}
        ticket_id = str(state["ticket_id"])
        trace_id = str(state["trace_id"])
        context = ToolContext(trace_id=trace_id, timeout_s=settings.tool_timeout_s)
        customer = registry.call(
            "lookup_customer", {"ticket_id": ticket_id},
            ticket_id=ticket_id, ctx=context,
        )
        if not customer.ok:
            return {
                "outcome": "escalated",
                "escalation": {
                    "reason_code": customer.error.code.value if customer.error else "TOOL_FAILURE",
                    "detail": "customer lookup failed before proposing an action",
                },
            }
        customer_id = str((customer.data or {}).get("customer_id"))
        text = f"{state.get('subject', '')}\n{state.get('body', '')}"
        if action == "refund":
            invoice_match = INVOICE_RE.search(text)
            if invoice_match is None:
                return {
                    "outcome": "escalated",
                    "escalation": {
                        "reason_code": "NEEDS_HUMAN_JUDGEMENT",
                        "detail": "refund request has no verifiable invoice reference",
                    },
                }
            invoice_id = invoice_match.group(0).upper()
            result = registry.call(
                "get_invoice",
                {"ticket_id": ticket_id, "customer_id": customer_id, "invoice_id": invoice_id},
                ticket_id=ticket_id, ctx=context,
            )
            if not result.ok:
                return {
                    "outcome": "escalated",
                    "escalation": {
                        "reason_code": result.error.code.value if result.error else "TOOL_FAILURE",
                        "detail": "invoice lookup failed before proposing a refund",
                    },
                }
            data = result.data or {}
            paid = sum(
                int(payment["amount_cents"])
                for payment in data.get("payments", [])
                if payment.get("status") == "succeeded"
            )
            refunded = sum(
                int(payment["amount_refunded_cents"])
                for payment in data.get("payments", [])
                if payment.get("status") == "succeeded"
            )
            matches = list(AMOUNT_RE.finditer(text))
            if len(matches) == 1:
                amount_cents = parse_amount_cents(text)
            elif not matches:
                amount_cents = paid - refunded  # no stated amount -> full remainder
            else:
                return {
                    "outcome": "escalated",
                    "escalation": {
                        "reason_code": "NEEDS_HUMAN_JUDGEMENT",
                        "detail": "several distinct dollar amounts in the ticket",
                    },
                }
            return {
                "action_context": {
                    "kind": "refund", "customer_id": customer_id,
                    "invoice_id": invoice_id, "amount_cents": amount_cents,
                    "invoice": data,
                }
            }
        subscription = registry.call(
            "get_subscription",
            {"ticket_id": ticket_id, "customer_id": customer_id},
            ticket_id=ticket_id, ctx=context,
        )
        if not subscription.ok:
            return {
                "outcome": "escalated",
                "escalation": {
                    "reason_code": subscription.error.code.value if subscription.error else "TOOL_FAILURE",
                    "detail": "subscription lookup failed before proposing a plan change",
                },
            }
        try:
            target = parse_target_plan(text)
        except ValueError:
            return {
                "outcome": "escalated",
                "escalation": {
                    "reason_code": "NEEDS_HUMAN_JUDGEMENT",
                    "detail": "plan change has no unambiguous target plan",
                },
            }
        return {
            "action_context": {
                "kind": "plan_change", "customer_id": customer_id,
                "to_plan_id": target, "subscription": subscription.data,
            }
        }

    return tool_executor


def make_propose_actions_node(registry: ToolRegistry):
    def propose_actions(state: AgentState) -> dict[str, Any]:
        context = state.get("action_context") or {}
        if context.get("kind") not in {"refund", "plan_change"}:
            return {"action_proposed": False}
        ticket_id = str(state["ticket_id"])
        tool_context = ToolContext(
            trace_id=str(state["trace_id"]), timeout_s=settings.tool_timeout_s
        )
        kb_ids = list(state.get("citations") or [])
        if not kb_ids:
            return {
                "outcome": "escalated",
                "escalation": {
                    "reason_code": "LOW_CONFIDENCE",
                    "detail": "an action requires grounded KB evidence",
                },
            }
        common = {
            "ticket_id": ticket_id,
            "customer_id": context["customer_id"],
            "reason": str(state.get("body") or "")[:1000],
            "kb_ids": kb_ids,
            "idempotency_key": f"agent-{ticket_id}-{context['kind']}",
        }
        if context["kind"] == "refund":
            args = {
                **common,
                "invoice_id": context["invoice_id"],
                "amount_cents": context["amount_cents"],
            }
            name = "propose_refund"
        else:
            args = {**common, "to_plan_id": context["to_plan_id"], "effective": "next_cycle"}
            name = "propose_plan_change"
        result = registry.call(name, args, ticket_id=ticket_id, ctx=tool_context)
        if not result.ok:
            return {
                "outcome": "escalated",
                "escalation": {
                    "reason_code": result.error.code.value if result.error else "TOOL_FAILURE",
                    "detail": "action proposal failed validation or a business rule",
                },
            }
        return {
            "action_proposed": True,
            "action": {"tool": name, "kind": context["kind"], "data": result.data},
        }

    return propose_actions


def make_failure_handler_node():
    def failure_handler(state: AgentState) -> dict[str, Any]:
        escalation = dict(state.get("escalation") or {})
        escalation.setdefault("reason_code", "INTERNAL_ERROR")
        escalation.setdefault("detail", "agent failure requires human review")
        return {"outcome": "escalated", "escalation": escalation}

    return failure_handler
