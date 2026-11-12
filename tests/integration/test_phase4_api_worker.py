"""Phase 4 API-worker approval replay and executor rollback coverage."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from psycopg.errors import RaiseException

from app.api.routes import decide, post_ticket
from app.api.schemas import DecisionBody, TicketCreate
from app.services.executor import execute_refund
from app.tools.registry import build_registry
from app.tools.schemas import ToolContext
from app.worker import run_one

pytestmark = pytest.mark.integration


def test_api_reply_approval_replays_and_worker_executes_once(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    payload = TicketCreate(
        subject="ITST API worker refund policy",
        body="Please explain the refund policy and the refund window.",
        from_email=two_customers.alice_email,
        from_name="Alice Sender",
    )
    created = post_ticket(payload, db_pool, f"itst-api-create-{uuid.uuid4().hex}")
    ticket_id = created.ticket_id

    for _ in range(10):
        run_one(db_pool, "itst-api-worker")
        status = migrated_conn.execute(
            "SELECT status FROM tickets WHERE id=%s", (ticket_id,)
        ).fetchone()["status"]
        if status != "triaging":
            break
    ticket = migrated_conn.execute(
        "SELECT status, draft_reply FROM tickets WHERE id=%s", (ticket_id,)
    ).fetchone()
    job = migrated_conn.execute(
        "SELECT status, attempts, last_error FROM jobs WHERE ticket_id=%s",
        (ticket_id,),
    ).fetchone()
    assert ticket["status"] == "awaiting_approval", job
    assert ticket["draft_reply"]
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE ticket_id=%s AND kind='run_agent'",
        (ticket_id,),
    ).fetchone()["n"] == 1

    decision = DecisionBody(decision="approve")
    first = decide(
        "send_reply", ticket_id, decision, None, db_pool,
        "mia", "itst-api-approval",
    )
    for _ in range(4):
        replay = decide(
            "send_reply", ticket_id, decision, None, db_pool,
            "mia", "itst-api-approval",
        )
        assert replay == first

    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE ticket_id=%s AND kind='resume_agent'",
        (ticket_id,),
    ).fetchone()["n"] == 1
    for _ in range(10):
        run_one(db_pool, "itst-api-worker-resume")
        if migrated_conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE ticket_id=%s", (ticket_id,)
        ).fetchone()["n"] == 1:
            break
    resume_job = migrated_conn.execute(
        "SELECT status, attempts, last_error FROM jobs "
        "WHERE ticket_id=%s AND kind='resume_agent'", (ticket_id,)
    ).fetchone()
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM outbox WHERE ticket_id=%s", (ticket_id,)
    ).fetchone()["n"] == 1, resume_job
    assert migrated_conn.execute(
        "SELECT status FROM tickets WHERE id=%s", (ticket_id,)
    ).fetchone()["status"] == "resolved"
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE ticket_id=%s AND status <> 'done'",
        (ticket_id,),
    ).fetchone()["n"] == 0


def test_refund_executor_rolls_back_on_post_update_failure(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket_id = str(
        migrated_conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (
                two_customers.alice_id,
                "ITST rollback refund",
                "Refund my invoice",
                f"itst-rollback-{uuid.uuid4().hex}",
                two_customers.alice_email,
            ),
        ).fetchone()["id"]
    )
    proposal = build_registry(db_pool).call(
        "propose_refund",
        {
            "ticket_id": ticket_id,
            "customer_id": str(two_customers.alice_id),
            "invoice_id": two_customers.inv_a,
            "amount_cents": 500,
            "reason": "Rollback test",
            "kb_ids": ["KB-006"],
            "idempotency_key": f"itst-rollback-{ticket_id}",
        },
        ticket_id=ticket_id,
        ctx=ToolContext(trace_id=f"rollback-{ticket_id}", timeout_s=5),
    )
    assert proposal.ok is True
    request_id = str(proposal.data["refund_request_id"])
    migrated_conn.execute(
        "INSERT INTO approvals (ticket_id, action_type, action_ref, decision, decided_by) "
        "VALUES (%s,'refund',%s,'approved','mia')",
        (ticket_id, request_id),
    )
    migrated_conn.execute(
        "UPDATE refund_requests SET status='approved', approved_by='mia' WHERE id=%s",
        (request_id,),
    )

    migrated_conn.execute(
        """
        CREATE OR REPLACE FUNCTION itst_fail_refund_audit()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.event = 'refund_executed' THEN
                RAISE EXCEPTION 'injected refund audit failure';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    migrated_conn.execute(
        "CREATE TRIGGER itst_fail_refund_audit_trigger "
        "BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION itst_fail_refund_audit()"
    )
    try:
        with pytest.raises(RaiseException, match="injected refund audit failure"):
            execute_refund(db_pool, request_id=request_id, approved_by="mia")
    finally:
        migrated_conn.execute("DROP TRIGGER IF EXISTS itst_fail_refund_audit_trigger ON audit_log")
        migrated_conn.execute("DROP FUNCTION IF EXISTS itst_fail_refund_audit()")

    assert migrated_conn.execute(
        "SELECT amount_refunded_cents FROM payments WHERE invoice_id=%s",
        (two_customers.inv_a,),
    ).fetchone()["amount_refunded_cents"] == 0
    assert migrated_conn.execute(
        "SELECT status FROM refund_requests WHERE id=%s", (request_id,)
    ).fetchone()["status"] == "approved"
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE entity='refund' AND entity_id=%s "
        "AND event='refund_executed'", (request_id,),
    ).fetchone()["n"] == 0


def test_refund_decision_api_executes_once_with_audit(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket_id = str(
        migrated_conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (
                two_customers.alice_id,
                "ITST API refund approval",
                "Please refund my invoice.",
                f"itst-api-refund-{uuid.uuid4().hex}",
                two_customers.alice_email,
            ),
        ).fetchone()["id"]
    )
    proposal = build_registry(db_pool).call(
        "propose_refund",
        {
            "ticket_id": ticket_id,
            "customer_id": str(two_customers.alice_id),
            "invoice_id": two_customers.inv_a,
            "amount_cents": 500,
            "reason": "API approval test",
            "kb_ids": ["KB-006"],
            "idempotency_key": f"itst-api-refund-{ticket_id}",
        },
        ticket_id=ticket_id,
        ctx=ToolContext(trace_id=f"api-refund-{ticket_id}", timeout_s=5),
    )
    request_id = str(proposal.data["refund_request_id"])
    before = migrated_conn.execute(
        "SELECT amount_refunded_cents FROM payments WHERE invoice_id=%s",
        (two_customers.inv_a,),
    ).fetchone()["amount_refunded_cents"]
    first = decide(
        "refund", request_id, DecisionBody(decision="approve"), None, db_pool,
        "mia", f"itst-refund-decision-{ticket_id}",
    )
    assert decide(
        "refund", request_id, DecisionBody(decision="approve"), None, db_pool,
        "mia", f"itst-refund-decision-{ticket_id}",
    ) == first
    after = migrated_conn.execute(
        "SELECT amount_refunded_cents FROM payments WHERE invoice_id=%s",
        (two_customers.inv_a,),
    ).fetchone()["amount_refunded_cents"]
    assert after - before == 500
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE entity='refund' AND entity_id=%s "
        "AND event IN ('refund_decision_recorded','refund_executed')",
        (request_id,),
    ).fetchone()["n"] == 2
