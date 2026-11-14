"""The whitelisted invoice record handed to the reply pipeline.

Only facts the reply may state are copied out of `get_invoice`; anything else
(internal ids, ticket text, customer PII) must not enter the prompt.
"""

from __future__ import annotations

from app.agent.record import RECORD_CLOSE, RECORD_OPEN, build_account_record, render_account_record


def _invoice() -> dict:
    return {
        "invoice_id": "INV-0008-M4",
        "status": "paid",
        "amount_cents": 2003,
        "currency": "USD",
        "issued_at": "2026-10-29T12:00:00+00:00",
        "due_at": "2026-11-12T12:00:00+00:00",
        "payments": [
            {"status": "succeeded", "amount_cents": 2003, "method": "visa_1111",
             "paid_at": "2026-10-29T18:00:00+00:00", "id": "secret-uuid"},
            {"status": "failed", "amount_cents": 1003, "method": "mc_5555", "paid_at": None},
        ],
        "customer_id": "leak-me",
        "internal_note": "should never reach a prompt",
    }


def test_record_keeps_only_stateable_facts() -> None:
    record = build_account_record(_invoice())
    assert set(record) == {
        "invoice_id", "invoice_status", "amount_cents", "currency",
        "issued_at", "due_at", "payments",
    }
    assert record["invoice_id"] == "INV-0008-M4"
    assert record["invoice_status"] == "paid"
    assert [p["status"] for p in record["payments"]] == ["succeeded", "failed"]
    # payment ids and customer/internal fields are dropped
    assert all("id" not in payment or payment.get("status") for payment in record["payments"])
    assert "customer_id" not in record and "internal_note" not in record


def test_rendered_block_is_delimited_and_names_each_payment() -> None:
    text = render_account_record(build_account_record(_invoice()))
    assert text.startswith(RECORD_OPEN)
    assert text.endswith(RECORD_CLOSE)
    assert "status=succeeded amount=2003" in text
    assert "status=failed amount=1003" in text
    assert "leak-me" not in text


def test_invoice_without_payments_says_so() -> None:
    text = render_account_record(build_account_record({
        "invoice_id": "INV-0001-M1", "status": "open", "amount_cents": 2000,
        "currency": "USD", "issued_at": None, "due_at": None, "payments": [],
    }))
    assert "payment: none recorded" in text
