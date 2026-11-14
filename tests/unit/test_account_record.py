"""The whitelisted account record handed to the reply pipeline.

Only facts the reply may state are copied out of `get_invoice` / `get_subscription`;
anything else (internal ids, ticket text, customer PII) must not enter the prompt.
"""

from __future__ import annotations

from app.agent.record import (
    RECORD_CLOSE,
    RECORD_OPEN,
    build_account_record,
    build_plan_record,
    render_account_record,
)


def _invoice() -> dict:
    return {
        "invoice_id": "INV-0008-M4",
        "status": "paid",
        "amount_cents": 2003,
        "currency": "USD",
        "issued_at": "2026-10-29T12:00:00+00:00",
        "due_at": "2026-11-12T12:00:00+00:00",
        "payments": [
            {"status": "succeeded", "amount_cents": 2003, "amount_refunded_cents": 601,
             "method": "visa_1111",
             "paid_at": "2026-10-29T18:00:00+00:00", "id": "secret-uuid"},
            {"status": "failed", "amount_cents": 1003, "amount_refunded_cents": 0,
             "method": "mc_5555", "paid_at": None},
        ],
        "customer_id": "leak-me",
        "internal_note": "should never reach a prompt",
    }


def test_record_keeps_only_stateable_facts() -> None:
    record = build_account_record(_invoice())
    assert set(record) == {
        "record_type", "invoice_id", "invoice_status", "amount_cents", "currency",
        "issued_at", "due_at", "payments",
    }
    assert record["record_type"] == "invoice"
    assert record["invoice_id"] == "INV-0008-M4"
    assert record["invoice_status"] == "paid"
    assert [p["status"] for p in record["payments"]] == ["succeeded", "failed"]
    assert [p["amount_refunded_cents"] for p in record["payments"]] == [601, 0]
    # payment ids and customer/internal fields are dropped
    assert all("id" not in payment or payment.get("status") for payment in record["payments"])
    assert "customer_id" not in record and "internal_note" not in record


def test_rendered_block_is_delimited_and_names_each_payment() -> None:
    text = render_account_record(build_account_record(_invoice()))
    assert text.startswith(RECORD_OPEN)
    assert text.endswith(RECORD_CLOSE)
    assert "status=succeeded amount=2003" in text
    assert "refunded=601" in text
    assert "status=failed amount=1003" in text
    assert "leak-me" not in text


def test_invoice_without_payments_says_so() -> None:
    text = render_account_record(build_account_record({
        "invoice_id": "INV-0001-M1", "status": "open", "amount_cents": 2000,
        "currency": "USD", "issued_at": None, "due_at": None, "payments": [],
    }))
    assert "payment: none recorded" in text


def test_refund_branch_carries_the_amount_about_to_be_proposed() -> None:
    record = build_account_record(_invoice(), requested_refund_cents=1402)
    text = render_account_record(record)
    assert record["requested_refund_cents"] == 1402
    assert "refund requested for approval: 1402 cents" in text


def test_plan_record_keeps_current_and_target_plan() -> None:
    record = build_plan_record(
        {"subscriptions": [
            {"plan_id": "PLAN-PRO", "status": "archived", "billing_cycle": "monthly",
             "current_period_end": None, "subscription_id": "secret-uuid"},
            {"plan_id": "PLAN-PRO", "status": "active", "billing_cycle": "annual",
             "current_period_end": "2026-12-01T00:00:00+00:00",
             "subscription_id": "secret-uuid"},
        ]},
        "PLAN-PRO",
        "PLAN-STARTER",
    )
    assert record == {
        "record_type": "plan",
        "current_plan_id": "PLAN-PRO",
        "target_plan_id": "PLAN-STARTER",
        "billing_cycle": "annual",
        "current_period_end": "2026-12-01T00:00:00+00:00",
    }
    text = render_account_record(record)
    assert text.startswith(RECORD_OPEN) and text.endswith(RECORD_CLOSE)
    assert "current_plan=PLAN-PRO" in text
    assert "target_plan=PLAN-STARTER" in text
    assert "secret-uuid" not in text
