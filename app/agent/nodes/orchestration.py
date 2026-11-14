"""Deterministic planning and narrow, validated action proposals."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.agent.nodes.common import trace_node
from app.agent.record import (
    build_account_record,
    build_customer_record,
    build_plan_record,
)
from app.agent.state import AgentState
from app.config import settings
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolContext

INVOICE_RE = re.compile(r"\bINV-\d{4,8}-[A-Z0-9]+\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\d,])")
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
# Account questions should read the sender's account record before answering.
# The status is only used to explain an escalation; it never authorizes an
# account-state change and is not passed to grounding/draft.
ACCOUNT_READ_CATEGORIES = {"login_account_access"}


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


def refund_proposal_is_eligible(invoice: dict[str, Any], amount_cents: int) -> bool:
    """Whether `propose_refund` will accept this proposal.

    A *presentation* hint, not a control: `propose_refund` re-checks every rule
    server-side. The draft runs before the proposal, so without this it could
    tell a customer a refund is under review and then have the tool refuse it
    (E-015, F19). Any doubt -> False, which only makes the draft quieter.
    """
    if invoice.get("status") not in ("paid", "partially_refunded"):
        return False
    payments = [
        payment for payment in invoice.get("payments") or []
        if payment.get("status") == "succeeded"
    ]
    paid = sum(int(payment.get("amount_cents") or 0) for payment in payments)
    refunded = sum(
        int(payment.get("amount_refunded_cents") or 0) for payment in payments
    )
    if amount_cents <= 0 or amount_cents > paid - refunded:
        return False
    paid_ats = [
        str(payment["paid_at"]) for payment in payments if payment.get("paid_at")
    ]
    if not paid_ats:
        return False
    try:
        newest = max(datetime.fromisoformat(value) for value in paid_ats)
        floor = settings.as_of - timedelta(days=settings.refund_window_days)
        return newest >= floor
    except (TypeError, ValueError):
        # unparseable or mixed-awareness timestamps: stay quiet, let the tool
        # decide (and the refusal path amend the draft)
        return False


def parse_target_plan(text: str, current_plan_id: str | None = None) -> str:
    """The plan the customer asked to move TO.

    One mentioned plan decides; with several, only a directional from/to
    reading decides, and only when it accounts for every mentioned plan —
    "from Pro to Starter, not Business" stays ambiguous. When the customer
    names their current plan as well ("I am on the annual Pro plan, move me to
    Starter"), the subscription supplies the missing "from", but only if
    exactly one mentioned plan differs from it."""
    mentioned = {match.group(1).lower() for match in PLAN_WORD_RE.finditer(text)}
    if len(mentioned) == 1:
        return PLAN_IDS[mentioned.pop()]
    to_from = _TO_FROM_RE.search(text)
    if to_from and mentioned <= {to_from.group(1).lower(), to_from.group(2).lower()}:
        return PLAN_IDS[to_from.group(1).lower()]
    from_to = _FROM_TO_RE.search(text)
    if from_to and mentioned <= {from_to.group(1).lower(), from_to.group(2).lower()}:
        return PLAN_IDS[from_to.group(2).lower()]
    if current_plan_id:
        others = [PLAN_IDS[word] for word in mentioned if PLAN_IDS[word] != current_plan_id]
        if len(others) == 1:
            return others[0]
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
        ticket_id = str(state["ticket_id"])
        trace_id = str(state["trace_id"])
        context = ToolContext(trace_id=trace_id, timeout_s=settings.tool_timeout_s)
        text = f"{state.get('subject', '')}\n{state.get('body', '')}"
        invoice_match = INVOICE_RE.search(text)

        if action not in {"refund", "plan_change"}:
            category = str((state.get("triage") or {}).get("category") or "")
            if invoice_match is None and category not in ACCOUNT_READ_CATEGORIES:
                return {"action_context": {"kind": "reply"}}
            # "Your system says the payment failed, but I see a charge" is a
            # question about one invoice, not about policy. An account question
            # ("is my account suspended?") is about one customer's status.
            # Read the record before answering; a foreign invoice or an
            # unresolvable sender escalates instead.
            customer = registry.call(
                "lookup_customer", {"ticket_id": ticket_id},
                ticket_id=ticket_id, ctx=context,
            )
            if not customer.ok:
                return {
                    "outcome": "escalated",
                    "escalation": {
                        "reason_code": (
                            customer.error.code.value if customer.error else "TOOL_FAILURE"
                        ),
                        "detail": (
                            "customer lookup failed before reading the invoice"
                            if invoice_match is not None
                            else "customer lookup failed before reading the account status"
                        ),
                    },
                }
            if invoice_match is None:
                return {
                    "action_context": {"kind": "reply"},
                    "customer_record": build_customer_record(customer.data or {}),
                }
            invoice = registry.call(
                "get_invoice",
                {
                    "ticket_id": ticket_id,
                    "customer_id": str((customer.data or {}).get("customer_id")),
                    "invoice_id": invoice_match.group(0).upper(),
                },
                ticket_id=ticket_id, ctx=context,
            )
            if not invoice.ok:
                return {
                    "outcome": "escalated",
                    "escalation": {
                        "reason_code": (
                            invoice.error.code.value if invoice.error else "TOOL_FAILURE"
                        ),
                        "detail": "invoice lookup failed for a question about that invoice",
                    },
                }
            return {
                "action_context": {
                    "kind": "reply",
                    "invoice_id": invoice_match.group(0).upper(),
                },
                "account_record": build_account_record(invoice.data or {}),
            }

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
        if action == "refund":
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
                try:
                    amount_cents = parse_amount_cents(text)
                except ValueError:
                    # parser failure must never fall through to INTERNAL_ERROR
                    return {
                        "outcome": "escalated",
                        "escalation": {
                            "reason_code": "NEEDS_HUMAN_JUDGEMENT",
                            "detail": "refund amount could not be parsed unambiguously",
                        },
                    }
            elif not matches:
                if "$" in text:
                    # a dollar sign is present but no parseable amount: do not
                    # guess a full refund from malformed text
                    return {
                        "outcome": "escalated",
                        "escalation": {
                            "reason_code": "NEEDS_HUMAN_JUDGEMENT",
                            "detail": "ticket mentions a dollar amount that cannot be parsed",
                        },
                    }
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
                },
                # The draft runs before the proposal, so it cannot look the
                # invoice up again. Hand it the same verified facts the reply
                # branch gets, plus the amount about to go up for approval
                # (F18: the old draft asked for a payment confirmation it had
                # just read). Only announce the amount when the tool's own
                # preconditions say it will be accepted; otherwise the draft
                # would promise a proposal the refusal path then denies (F19).
                "account_record": build_account_record(
                    data,
                    requested_refund_cents=(
                        amount_cents
                        if refund_proposal_is_eligible(data, amount_cents)
                        else None
                    ),
                ),
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
        active_plan = next(
            (
                str(entry["plan_id"])
                for entry in (subscription.data or {}).get("subscriptions") or []
                if entry.get("status") == "active"
            ),
            None,
        )
        try:
            target = parse_target_plan(text, current_plan_id=active_plan)
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
            },
            # Same reason as the refund branch: let the draft name the current
            # and requested plan instead of asking the customer which one they
            # are on.
            "account_record": build_plan_record(
                subscription.data or {}, active_plan, target
            ),
        }

    return tool_executor


def refusal_sentence(kind: str, code: str | None) -> str:
    """One deterministic, customer-safe line for a refused action.

    The draft is written before the action is attempted, so it can read as if a
    proposal exists ("once the proposal is reviewed..."). When the tool then
    refuses, that is misleading to the operator. This sentence is appended to
    the persisted draft instead of spending another LLM call, and it never
    repeats internal numbers (cents, balances) back to the customer.
    """
    subject = "this refund" if kind == "refund" else "this plan change"
    if code == "business_rule":
        return (
            f"We couldn't create {subject} automatically because it doesn't meet the "
            "applicable policy. A support specialist will review your request and follow up."
        )
    if code == "ownership_mismatch":
        return (
            "We couldn't verify this request against your account, so a support specialist "
            "will review it."
        )
    if code == "duplicate":
        return (
            "There is already an open request for this change. A support specialist will "
            "review it."
        )
    return (
        "We couldn't complete this automatically. A support specialist will review your "
        "request."
    )


def make_propose_actions_node(registry: ToolRegistry, pool: Any | None = None):
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

        def refuse(code: str, detail: str) -> dict[str, Any]:
            """Escalate with a policy reason and amend the persisted draft.

            The draft node has already written the model text; without this the
            UI shows a proposal that was never created. The decision row carries
            the sentence so `/trace` can show *which* rule refused the action.
            """
            base = str(state.get("draft") or "").strip()
            refusal = refusal_sentence(str(context["kind"]), code)
            amended = f"{base}\n\n{refusal}" if base else refusal
            if pool is not None:
                with pool.connection() as conn:
                    conn.execute(
                        "UPDATE tickets SET draft_reply = %s WHERE id = %s",
                        (amended, ticket_id),
                    )
                trace_node(
                    pool, ticket_id, str(state["trace_id"]), name, "decision",
                    {"code": code},
                    {"error": {"code": code, "message": detail, "retryable": False}},
                    status="error", error=code,
                )
            return {
                "outcome": "escalated",
                "escalation": {"reason_code": code, "detail": detail},
                "draft": amended,
            }

        # An invoice with nothing left to refund cannot produce a valid proposal
        # (`amount_cents` is a PositiveInt); catch it here so the reason is a
        # policy rule, not a validation error. The draft is still amended.
        if context["kind"] == "refund" and int(context.get("amount_cents") or 0) <= 0:
            return refuse(
                "business_rule",
                "nothing left to refund on this invoice (already fully refunded)",
            )

        result = registry.call(name, args, ticket_id=ticket_id, ctx=tool_context)
        if not result.ok:
            return refuse(
                result.error.code.value if result.error else "TOOL_FAILURE",
                f"action proposal refused: "
                f"{result.error.message if result.error else 'tool failure'}",
            )
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
