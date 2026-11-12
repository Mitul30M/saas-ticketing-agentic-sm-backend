"""P3-3: refund proposal business rules verified against Postgres.

Skips without DB. Window arithmetic is against the injected settings.as_of
(2026-11-10T12:00Z), never now() — AGENTS invariant 12. Window semantics:
floor = as_of - refund_window_days; paid_at == floor is INSIDE (inclusive);
one second earlier is outside."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from app.config import settings
from app.tools.registry import build_registry
from app.tools.schemas import ToolContext, ToolErrorCode

pytestmark = pytest.mark.integration


@pytest.fixture()
def registry(db_pool: Any) -> Any:
    return build_registry(db_pool)


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext(trace_id=f"it-{uuid.uuid4().hex[:8]}", timeout_s=5)


def _ticket_for(
    migrated_conn: Any, two_customers: Any, invoice: str, body: str
) -> str:
    """A fresh ITST ticket sent from alice's address about `invoice`."""
    return str(
        migrated_conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (
                two_customers.alice_id,
                f"ITST refund tool {uuid.uuid4().hex[:8]}",
                body,
                f"itst-refund-{uuid.uuid4().hex}",
                two_customers.alice_email,
            ),
        ).fetchone()["id"]
    )


def _refund_args(
    t: Any, ticket_id: str, invoice: str, amount_cents: int
) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "customer_id": str(t.alice_id),
        "invoice_id": invoice,
        "amount_cents": amount_cents,
        "reason": "Customer requests a refund for the failed charge",
        "kb_ids": ["KB-006", "KB-009"],
        "idempotency_key": f"agent-{ticket_id}-refund",
    }


def _refund_rows(migrated_conn: Any, invoice: str) -> list[dict[str, Any]]:
    return migrated_conn.execute(
        "SELECT ticket_id, amount_cents, status, evidence, idempotency_key "
        "FROM refund_requests WHERE invoice_id = %s",
        (invoice,),
    ).fetchall()


# ---- success + evidence ---------------------------------------------------------


def test_refund_success_creates_pending_row_with_evidence(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    ticket = _ticket_for(migrated_conn, t, t.inv_a, f"Refund invoice {t.inv_a} please")

    result = registry.call(
        "propose_refund", _refund_args(t, ticket, t.inv_a, 500),
        ticket_id=ticket, ctx=ctx,
    )

    assert result.ok is True
    assert result.data["status"] == "pending_approval"
    row = _refund_rows(migrated_conn, t.inv_a)[0]
    assert row["status"] == "pending_approval"
    assert row["amount_cents"] == 500
    assert set(row["evidence"]["kb_ids"]) == {"KB-006", "KB-009"}
    assert row["evidence"]["refundable_cents"] == 5000


def test_agent_idempotency_key_is_metadata_only(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    ticket = _ticket_for(migrated_conn, t, t.inv_a, f"Refund invoice {t.inv_a}")
    args = _refund_args(t, ticket, t.inv_a, 500)
    claimed = args["idempotency_key"]

    result = registry.call("propose_refund", args, ticket_id=ticket, ctx=ctx)

    assert result.ok is True
    row = _refund_rows(migrated_conn, t.inv_a)[0]
    assert row["idempotency_key"].startswith("srv-")
    assert row["evidence"]["claimed_idempotency_key"] == claimed


# ---- amount caps ----------------------------------------------------------------


def test_amount_equal_to_refundable_passes(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    ticket = _ticket_for(migrated_conn, t, t.inv_a, f"Refund all of {t.inv_a}")

    result = registry.call(
        "propose_refund", _refund_args(t, ticket, t.inv_a, 5000),
        ticket_id=ticket, ctx=ctx,
    )

    assert result.ok is True


def test_amount_one_cent_above_refundable_is_business_rule(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    ticket = _ticket_for(migrated_conn, t, t.inv_a, f"Refund all of {t.inv_a}")

    result = registry.call(
        "propose_refund", _refund_args(t, ticket, t.inv_a, 5001),
        ticket_id=ticket, ctx=ctx,
    )

    assert result.ok is False
    assert result.error.code == ToolErrorCode.BUSINESS_RULE
    assert _refund_rows(migrated_conn, t.inv_a) == []


def test_partially_refunded_invoice_caps_correctly(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    migrated_conn.execute(
        "UPDATE payments SET amount_refunded_cents = 1000 WHERE invoice_id = %s",
        (t.inv_a,),
    )
    first = _ticket_for(migrated_conn, t, t.inv_a, f"Refund rest of {t.inv_a}")

    ok = registry.call(
        "propose_refund", _refund_args(t, first, t.inv_a, 4000),
        ticket_id=first, ctx=ctx,
    )
    assert ok.ok is True

    # the invoice now has an open request; a second ticket with a VALID amount
    # still gets DUPLICATE (one open request per invoice)
    second = _ticket_for(migrated_conn, t, t.inv_a, f"Refund rest of {t.inv_a} again")
    over = registry.call(
        "propose_refund", _refund_args(t, second, t.inv_a, 4000),
        ticket_id=second, ctx=ctx,
    )
    assert over.ok is False
    assert over.error.code == ToolErrorCode.DUPLICATE


# ---- payment + invoice status ---------------------------------------------------


def test_owned_invoice_without_succeeded_payment_is_business_rule(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    migrated_conn.execute(
        "UPDATE payments SET status = 'failed' WHERE invoice_id = %s", (t.inv_a,)
    )
    ticket = _ticket_for(migrated_conn, t, t.inv_a, f"Refund {t.inv_a}")

    result = registry.call(
        "propose_refund", _refund_args(t, ticket, t.inv_a, 500),
        ticket_id=ticket, ctx=ctx,
    )

    assert result.ok is False
    assert result.error.code == ToolErrorCode.BUSINESS_RULE
    assert _refund_rows(migrated_conn, t.inv_a) == []


def test_unpaid_invoice_status_is_business_rule(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    migrated_conn.execute(
        "UPDATE invoices SET status = 'open' WHERE id = %s", (t.inv_a,)
    )
    ticket = _ticket_for(migrated_conn, t, t.inv_a, f"Refund {t.inv_a}")

    result = registry.call(
        "propose_refund", _refund_args(t, ticket, t.inv_a, 500),
        ticket_id=ticket, ctx=ctx,
    )

    assert result.ok is False
    assert result.error.code == ToolErrorCode.BUSINESS_RULE


# ---- refund window (against the injected AS_OF clock) ---------------------------


@pytest.mark.parametrize(
    ("days_before_as_of", "expected_ok"),
    [(29, True), (30, True), (31, False)],
    ids=["inside", "boundary-inclusive", "outside"],
)
def test_refund_window_boundary_on_paid_at(
    registry, migrated_conn, two_customers, ctx, days_before_as_of: int, expected_ok: bool
) -> None:
    t = two_customers
    paid_at = settings.as_of - timedelta(days=days_before_as_of)
    migrated_conn.execute(
        "UPDATE payments SET paid_at = %s WHERE invoice_id = %s", (paid_at, t.inv_a)
    )
    ticket = _ticket_for(migrated_conn, t, t.inv_a, f"Refund {t.inv_a}")

    result = registry.call(
        "propose_refund", _refund_args(t, ticket, t.inv_a, 500),
        ticket_id=ticket, ctx=ctx,
    )

    assert result.ok is expected_ok
    if not expected_ok:
        assert result.error.code == ToolErrorCode.BUSINESS_RULE
        assert _refund_rows(migrated_conn, t.inv_a) == []


# ---- duplicates -----------------------------------------------------------------


def test_second_open_request_for_same_invoice_is_duplicate(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    first = _ticket_for(migrated_conn, t, t.inv_a, f"Refund {t.inv_a}")
    second = _ticket_for(migrated_conn, t, t.inv_a, f"Refund {t.inv_a} again")

    ok = registry.call(
        "propose_refund", _refund_args(t, first, t.inv_a, 500),
        ticket_id=first, ctx=ctx,
    )
    dup = registry.call(
        "propose_refund", _refund_args(t, second, t.inv_a, 300),
        ticket_id=second, ctx=ctx,
    )

    assert ok.ok is True
    assert dup.ok is False
    assert dup.error.code == ToolErrorCode.DUPLICATE
    assert len(_refund_rows(migrated_conn, t.inv_a)) == 1


def test_identical_replay_is_duplicate_and_stays_one_row(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    ticket = _ticket_for(migrated_conn, t, t.inv_a, f"Refund {t.inv_a}")
    args = _refund_args(t, ticket, t.inv_a, 500)

    first = registry.call("propose_refund", args, ticket_id=ticket, ctx=ctx)
    replay = registry.call("propose_refund", args, ticket_id=ticket, ctx=ctx)

    assert first.ok is True
    assert replay.ok is False
    assert replay.error.code == ToolErrorCode.DUPLICATE
    assert len(_refund_rows(migrated_conn, t.inv_a)) == 1
