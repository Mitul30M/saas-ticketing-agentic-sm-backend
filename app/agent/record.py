"""Compacted account records handed to the reply pipeline.

A ticket like "your system says the payment failed, but I see a charge" is a
question about *one invoice*, not about policy. The reply branch reads that
invoice before drafting, so the model can state verified facts instead of
generic advice.

The record is a whitelisted projection of the `get_invoice` tool output —
statuses, amounts, methods and dates only. No internal ids beyond the invoice,
no customer PII, no free text from the ticket.
"""

from __future__ import annotations

from typing import Any

RECORD_OPEN = "<<<ACCOUNT_RECORD"
RECORD_CLOSE = "ACCOUNT_RECORD>>>"
RECORD_EVIDENCE_ID = "record"


def build_account_record(invoice: dict[str, Any]) -> dict[str, Any]:
    """Whitelist the invoice fields the reply is allowed to state."""
    payments = [
        {
            "status": payment.get("status"),
            "amount_cents": payment.get("amount_cents"),
            "method": payment.get("method"),
            "paid_at": payment.get("paid_at"),
        }
        for payment in invoice.get("payments") or []
    ]
    return {
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


def render_account_record(record: dict[str, Any]) -> str:
    """Prompt block: plainly delimited data, never instructions."""
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
            f"cents method={payment.get('method')} paid_at={payment.get('paid_at')}"
        )
    if not record.get("payments"):
        lines.append("payment: none recorded")
    lines.append(RECORD_CLOSE)
    return "\n".join(lines)
