"""Ownership + trace integration tests for the real tools (AGENTS invariants 4, 9, 11).

Needs the compose Postgres (skips otherwise). The names `build_registry`,
`get_invoice`, `lookup_customer`, `propose_refund` are the agreed contract.

Threat model: a customer emails from their OWN address but the body (or a model that
read it) names SOMEONE ELSE's customer id / invoice / email. The tool must resolve
identity from tickets.from_email in the DB and ignore anything claimed in the
arguments. Fixtures live in tests/integration/conftest.py (`two_customers`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.tools.registry import build_registry
from app.tools.schemas import ToolContext, ToolErrorCode

pytestmark = pytest.mark.integration


@pytest.fixture()
def registry(db_pool: Any) -> Any:
    return build_registry(db_pool)


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext(trace_id=f"it-{uuid.uuid4().hex[:8]}", timeout_s=5)


def _trace_rows(conn: Any, trace_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT node, kind, status, error, duration_ms FROM trace_events "
        "WHERE trace_id = %s ORDER BY id",
        (trace_id,),
    ).fetchall()


def _refund_count(conn: Any) -> int:
    return conn.execute("SELECT count(*) AS n FROM refund_requests").fetchone()["n"]


# --------------------------------------------------------------------------- identity from sender
def test_lookup_customer_uses_ticket_sender_not_arguments(
    registry, migrated_conn, two_customers, ctx
) -> None:
    """Even if the model passes bob's email, the customer is whoever the ticket was sent FROM."""
    t = two_customers
    result = registry.call(
        "lookup_customer",
        {"ticket_id": t.ticket_id, "from_email": t.bob_email},  # claimed identity: ignored
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert result.ok is True
    assert result.data["customer_id"] == str(t.alice_id)


def test_lookup_customer_unknown_sender_is_not_found(registry, migrated_conn, two_customers, ctx) -> None:
    t = two_customers
    migrated_conn.execute(
        "UPDATE tickets SET from_email = 'stranger@nowhere.test' WHERE id = %s", (t.ticket_id,)
    )

    result = registry.call("lookup_customer", {"ticket_id": t.ticket_id}, ticket_id=t.ticket_id, ctx=ctx)

    assert result.ok is False
    assert result.error.code == ToolErrorCode.NOT_FOUND
    assert result.data is None


def test_sender_match_is_case_insensitive(registry, migrated_conn, two_customers, ctx) -> None:
    """Design assumption: email addresses compare case-insensitively. Delete if you decide otherwise."""
    t = two_customers
    migrated_conn.execute(
        "UPDATE tickets SET from_email = upper(from_email) WHERE id = %s", (t.ticket_id,)
    )

    result = registry.call("lookup_customer", {"ticket_id": t.ticket_id}, ticket_id=t.ticket_id, ctx=ctx)

    assert result.ok is True
    assert result.data["customer_id"] == str(t.alice_id)


# --------------------------------------------------------------------------- ownership failures
def test_own_invoice_is_returned(registry, two_customers, ctx) -> None:
    t = two_customers
    result = registry.call(
        "get_invoice",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id), "invoice_id": t.inv_a},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert result.ok is True
    assert result.data["invoice_id"] == t.inv_a


def test_another_customers_invoice_is_ownership_mismatch(registry, two_customers, ctx) -> None:
    """Sender is alice; she asks about bob's invoice using HER OWN customer id."""
    t = two_customers
    result = registry.call(
        "get_invoice",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id), "invoice_id": t.inv_b},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert result.ok is False
    assert result.error.code == ToolErrorCode.OWNERSHIP_MISMATCH
    assert result.error.retryable is False
    assert result.data is None


def test_claimed_customer_id_of_someone_else_is_ownership_mismatch(registry, two_customers, ctx) -> None:
    """The attack that matters: alice writes from her address but claims bob's customer id AND
    bob's invoice. Everything is internally consistent for bob, so only the sender check stops it."""
    t = two_customers
    result = registry.call(
        "get_invoice",
        {
            "ticket_id": t.ticket_id,
            "from_email": t.bob_email,  # claimed, must be ignored or rejected
            "customer_id": str(t.bob_id),
            "invoice_id": t.inv_b,
        },
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert result.ok is False
    assert result.error.code == ToolErrorCode.OWNERSHIP_MISMATCH
    assert result.data is None


def test_ownership_error_does_not_leak_the_other_customers_data(registry, two_customers, ctx) -> None:
    t = two_customers
    result = registry.call(
        "get_invoice",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id), "invoice_id": t.inv_b},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    text = result.model_dump_json()
    for secret in (t.bob_email, t.bob_name, str(t.bob_id), str(t.bob_amount_cents)):
        assert secret not in text


def test_nonexistent_invoice_and_foreign_invoice_look_alike_to_the_caller(
    registry, two_customers, ctx
) -> None:
    """Optional hardening: otherwise a model/attacker can probe which invoice ids exist.
    If you choose NOT_FOUND for missing and OWNERSHIP_MISMATCH for foreign, delete this test."""
    t = two_customers
    missing = registry.call(
        "get_invoice",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id), "invoice_id": "INV-9999-ZZ"},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )
    foreign = registry.call(
        "get_invoice",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id), "invoice_id": t.inv_b},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert missing.error.code == foreign.error.code


def test_refund_proposal_for_another_customers_invoice_writes_nothing(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    before = _refund_count(migrated_conn)

    result = registry.call(
        "propose_refund",
        {
            "ticket_id": t.ticket_id,
            "customer_id": str(t.alice_id),
            "invoice_id": t.inv_b,
            "amount_cents": 500,
            "reason": "Please refund this charge",
            "kb_ids": ["KB-006"],
            "idempotency_key": "own-key-0001",
        },
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert result.ok is False
    assert result.error.code == ToolErrorCode.OWNERSHIP_MISMATCH
    assert _refund_count(migrated_conn) == before  # the gate held: no row, not even pending


# --------------------------------------------------------------------------- trace row written (real DB)
def test_ownership_failure_writes_one_error_trace_row(registry, migrated_conn, two_customers, ctx) -> None:
    t = two_customers

    registry.call(
        "get_invoice",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id), "invoice_id": t.inv_b},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    rows = _trace_rows(migrated_conn, ctx.trace_id)
    assert len(rows) == 1
    assert rows[0]["node"] == "get_invoice"
    assert rows[0]["kind"] == "tool"
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "ownership_mismatch"
    assert rows[0]["duration_ms"] is not None


def test_successful_call_writes_one_ok_trace_row(registry, migrated_conn, two_customers, ctx) -> None:
    t = two_customers

    registry.call(
        "get_invoice",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id), "invoice_id": t.inv_a},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    rows = _trace_rows(migrated_conn, ctx.trace_id)
    assert [(r["node"], r["status"]) for r in rows] == [("get_invoice", "ok")]


def test_invalid_input_trace_row_exists_and_no_db_write_happened(
    registry, migrated_conn, two_customers, ctx
) -> None:
    t = two_customers
    before = _refund_count(migrated_conn)

    registry.call(
        "propose_refund",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id), "invoice_id": "INV-1; DROP TABLE x"},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    rows = _trace_rows(migrated_conn, ctx.trace_id)
    assert [(r["status"], r["error"]) for r in rows] == [("error", "invalid_input")]
    assert _refund_count(migrated_conn) == before
