"""Dashboard projection: guarded status transitions and persisted labels."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services.ticket_state import apply_run_result, mark_resolved
from app.tools.registry import ToolFailure

pytestmark = pytest.mark.integration


def _ticket(conn: Any, customer: Any, status: str = "new") -> str:
    return str(
        conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, status) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                customer.alice_id,
                f"ITST state {uuid.uuid4().hex[:8]}",
                "How does the refund policy work?",
                f"itst-state-{uuid.uuid4().hex}",
                customer.alice_email,
                status,
            ),
        ).fetchone()["id"]
    )


def _row(conn: Any, ticket: str) -> dict[str, Any]:
    return conn.execute(
        "SELECT status, agent_outcome, handled_by, escalated, escalation_reason, kb_supported, "
        "draft_citations, flags, triage_reasons FROM tickets WHERE id=%s", (ticket,)
    ).fetchone()


def test_interrupt_projects_an_approval_pause(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = _ticket(migrated_conn, two_customers, status="triaging")
    result = {
        "ticket_id": ticket,
        "thread_id": f"ticket:{ticket}",
        "__interrupt__": [{"value": {"kind": "send_reply"}}],
        "triage": {"reasons": {"category": "billing"}},
        "grounding": {"supported": True},
        "citations": ["KB-006"],
        "flags": ["injection_suspected"],
    }
    apply_run_result(db_pool, ticket, result)
    row = _row(migrated_conn, ticket)
    assert row["status"] == "awaiting_approval"
    assert row["agent_outcome"] == "action_proposed"
    assert row["handled_by"] == "agent"
    assert row["kb_supported"] is True
    assert row["draft_citations"] == ["KB-006"]
    assert row["flags"] == ["injection_suspected"]
    assert row["triage_reasons"] == {"category": "billing"}

    # replaying the same projection is harmless
    apply_run_result(db_pool, ticket, result)
    assert _row(migrated_conn, ticket)["status"] == "awaiting_approval"

    mark_resolved(db_pool, ticket)
    resolved = _row(migrated_conn, ticket)
    assert resolved["status"] == "resolved"
    assert resolved["agent_outcome"] == "action_proposed"
    assert resolved["handled_by"] == "agent"
    assert resolved["escalation_reason"] is None
    mark_resolved(db_pool, ticket)  # idempotent


def test_a_successful_rerun_clears_a_stale_escalation(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = _ticket(migrated_conn, two_customers, status="triaging")
    apply_run_result(
        db_pool, ticket,
        {"outcome": "escalated", "escalation": {"reason_code": "timeout"}},
    )
    assert _row(migrated_conn, ticket)["escalated"] is True

    migrated_conn.execute("UPDATE tickets SET status='triaging' WHERE id=%s", (ticket,))
    apply_run_result(
        db_pool, ticket,
        {"__interrupt__": [{"value": {"kind": "send_reply"}}], "approval": {}},
    )
    row = _row(migrated_conn, ticket)
    assert row["status"] == "awaiting_approval"
    assert row["escalated"] is False
    assert row["escalation_reason"] is None


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ({"outcome": "escalated", "escalation": {"reason_code": "NOT_IN_KB"}}, "escalated"),
        ({"failure": True, "outcome": "escalated",
          "escalation": {"reason_code": "INTERNAL_ERROR"}}, "failed"),
    ],
)
def test_escalation_and_failure_park_the_ticket_for_a_human(
    db_pool: Any, migrated_conn: Any, two_customers: Any, result: dict[str, Any], outcome: str
) -> None:
    ticket = _ticket(migrated_conn, two_customers, status="triaging")
    apply_run_result(db_pool, ticket, result)
    row = _row(migrated_conn, ticket)
    assert row["status"] == "awaiting_human"
    assert row["escalated"] is True
    assert row["agent_outcome"] == outcome
    assert row["escalation_reason"] == result["escalation"]["reason_code"]


def test_illegal_regression_is_rejected(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = _ticket(migrated_conn, two_customers, status="new")
    with pytest.raises(ToolFailure):
        apply_run_result(db_pool, ticket, {"outcome": "escalated",
                                           "escalation": {"reason_code": "NOT_IN_KB"}})
    assert _row(migrated_conn, ticket)["status"] == "new"

    migrated_conn.execute("UPDATE tickets SET status='resolved' WHERE id=%s", (ticket,))
    with pytest.raises(ToolFailure):
        apply_run_result(db_pool, ticket, {"__interrupt__": [{"value": {}}]})
