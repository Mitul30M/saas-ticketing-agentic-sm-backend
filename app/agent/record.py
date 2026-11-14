"""Compacted account records handed to the reply pipeline.

A ticket like "your system says the payment failed, but I see a charge" is a
question about *one invoice*, not about policy. The reply branch reads that
invoice before drafting, so the model can state verified facts instead of
generic advice. Refund and plan-change tickets read the same record before
drafting, so the draft can state what it already knows (and the amount being
put up for approval) instead of asking the customer to repeat it (F18).

A record is a whitelisted projection of a tool output — invoice status,
amounts, methods and dates, or the current/target plan. No internal ids beyond
the invoice, no customer PII, no free text from the ticket.
"""

from __future__ import annotations

from typing import Any

RECORD_OPEN = "<<<ACCOUNT_RECORD"
RECORD_CLOSE = "ACCOUNT_RECORD>>>"
RECORD_EVIDENCE_ID = "record"


def build_account_record(
    invoice: dict[str, Any], *, requested_refund_cents: int | None = None
) -> dict[str, Any]:
    """Whitelist the invoice fields the reply is allowed to state.

    `requested_refund_cents` is the amount the refund branch is about to put up
    for approval. It is computed deterministically from the invoice (or parsed
    from the ticket) and is not a promise: the draft may only say it is
    *proposed for approval*."""
    payments = [
        {
            "status": payment.get("status"),
            "amount_cents": payment.get("amount_cents"),
            "amount_refunded_cents": payment.get("amount_refunded_cents"),
            "method": payment.get("method"),
            "paid_at": payment.get("paid_at"),
        }
        for payment in invoice.get("payments") or []
    ]
    record: dict[str, Any] = {
        "record_type": "invoice",
        # `get_invoice` returns `invoice_id`/`status`; keep the mapping explicit so
        # a renamed tool field fails here instead of silently emptying the record.
        "invoice_id": invoice.get("invoice_id"),
        "invoice_status": invoice.get("status"),
        "amount_cents": invoice.get("amount_cents"),
        "currency": invoice.get("currency"),
        "issued_at": invoice.get("issued_at"),
        "due_at": invoice.get("due_at"),
        "payments": payments,
    }
    if requested_refund_cents is not None:
        record["requested_refund_cents"] = requested_refund_cents
    return record


def build_customer_record(customer: dict[str, Any]) -> dict[str, Any]:
    """Whitelist the account-status fields used to explain an escalation.

    This record is *not* passed to grounding or draft. It exists so an account
    question that the KB cannot answer still says what the verified account
    status is; the decision stays an escalation (no account-state change).
    """
    return {
        "record_type": "customer",
        "status": customer.get("status"),
    }


def build_plan_record(
    subscription: dict[str, Any], current_plan_id: str | None, target_plan_id: str
) -> dict[str, Any]:
    """Whitelist the subscription fields a plan-change draft may state."""
    active: dict[str, Any] = next(
        (
            entry
            for entry in subscription.get("subscriptions") or []
            if entry.get("status") == "active"
        ),
        {},
    )
    return {
        "record_type": "plan",
        "current_plan_id": current_plan_id or active.get("plan_id"),
        "target_plan_id": target_plan_id,
        "billing_cycle": active.get("billing_cycle"),
        "current_period_end": active.get("current_period_end"),
    }


def render_account_record(record: dict[str, Any]) -> str:
    """Prompt block: plainly delimited data, never instructions."""
    if record.get("record_type") == "plan":
        lines = [
            (
                f"{RECORD_OPEN}\n"
                f"subscription: current_plan={record.get('current_plan_id')} "
                f"target_plan={record.get('target_plan_id')} "
                f"billing_cycle={record.get('billing_cycle')} "
                f"current_period_end={record.get('current_period_end')}"
            )
        ]
        lines.append(RECORD_CLOSE)
        return "\n".join(lines)

    lines = [
        (
            f"{RECORD_OPEN}\n"
            f"invoice {record.get('invoice_id')}: status={record.get('invoice_status')} "
            f"total={record.get('amount_cents')} cents currency={record.get('currency')} "
            f"issued_at={record.get('issued_at')} due_at={record.get('due_at')}"
        )
    ]
    for payment in record.get("payments") or []:
        lines.append(
            f"payment: status={payment.get('status')} amount={payment.get('amount_cents')} "
            f"cents refunded={payment.get('amount_refunded_cents')} cents "
            f"method={payment.get('method')} paid_at={payment.get('paid_at')}"
        )
    if not record.get("payments"):
        lines.append("payment: none recorded")
    if record.get("requested_refund_cents") is not None:
        lines.append(
            f"refund requested for approval: {record.get('requested_refund_cents')} cents"
        )
    lines.append(RECORD_CLOSE)
    return "\n".join(lines)
