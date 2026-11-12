"""Resume-path convergence: retries send once, run-scoped drafts, replays."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app import worker
from app.api.routes import decide, post_ticket
from app.api.schemas import DecisionBody, TicketCreate
from app.services.approvals import record_reply_decision

pytestmark = pytest.mark.integration


def _create_and_pause(pool: Any, conn: Any, two_customers: Any) -> str:
    created = post_ticket(
        TicketCreate(
            subject="ITST resume refund policy",
            body="Please explain the refund policy and the refund window.",
            from_email=two_customers.alice_email,
        ),
        pool,
        f"itst-resume-create-{uuid.uuid4().hex}",
    )
    for _ in range(10):
        worker.run_one(pool, "itst-resume-worker")
        status = conn.execute(
            "SELECT status FROM tickets WHERE id=%s", (created.ticket_id,)
        ).fetchone()["status"]
        if status not in {"new", "triaging"}:
            break
    assert status == "awaiting_approval"
    return created.ticket_id


def _run_until(pool: Any, conn: Any, ticket: str, check: str, limit: int = 10) -> None:
    for _ in range(limit):
        worker.run_one(pool, "itst-resume-worker")
        if conn.execute(check, (ticket,)).fetchone()["n"]:
            return
    raise AssertionError(f"condition not reached: {check}")


def test_resume_retry_sends_exactly_one_reply(
    db_pool: Any, migrated_conn: Any, two_customers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _create_and_pause(db_pool, migrated_conn, two_customers)
    decide(
        "send_reply", ticket, DecisionBody(decision="approve"), None, db_pool, "mia",
        f"itst-resume-decide-{ticket}",
    )

    def boom(**_: Any) -> str:
        raise RuntimeError("transient outbox failure")

    monkeypatch.setattr(worker, "execute_approved_reply", boom)
    assert worker.run_one(db_pool, "itst-resume-worker") is True
    failed = migrated_conn.execute(
        "SELECT status, attempts FROM jobs WHERE ticket_id=%s AND kind='resume_agent'",
        (ticket,),
    ).fetchone()
    assert failed["status"] == "queued", "a non-terminal failure must be retried"
    assert int(failed["attempts"]) == 1
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM outbox WHERE ticket_id=%s", (ticket,)
    ).fetchone()["n"] == 0

    # make the backoff due and retry with the real executor
    migrated_conn.execute(
        "UPDATE jobs SET run_after=now() WHERE ticket_id=%s AND kind='resume_agent'", (ticket,)
    )
    monkeypatch.undo()
    _run_until(
        db_pool, migrated_conn, ticket,
        "SELECT count(*) AS n FROM outbox WHERE ticket_id=%s",
    )
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM outbox WHERE ticket_id=%s", (ticket,)
    ).fetchone()["n"] == 1
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE event='reply_executed' AND entity_id=%s",
        (ticket,),
    ).fetchone()["n"] == 1
    assert migrated_conn.execute(
        "SELECT status FROM jobs WHERE ticket_id=%s AND kind='resume_agent'", (ticket,)
    ).fetchone()["status"] == "done"


def test_identical_draft_on_a_new_run_can_be_approved(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = str(
        migrated_conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, "
            "status, draft_reply, thread_id) VALUES (%s,%s,%s,%s,%s,'awaiting_approval',%s,%s) "
            "RETURNING id",
            (
                two_customers.alice_id,
                f"ITST rerun {uuid.uuid4().hex[:8]}",
                "How does the refund policy work?",
                f"itst-rerun-{uuid.uuid4().hex}",
                two_customers.alice_email,
                "The refund policy allows returns within 30 days [KB-006].",
                f"ticket:{uuid.uuid4()}:run:1",
            ),
        ).fetchone()["id"]
    )
    rejected = record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="rejected"
    )
    # a re-run produces a new thread but the SAME cached draft text
    migrated_conn.execute(
        "UPDATE tickets SET thread_id=%s WHERE id=%s", (f"ticket:{ticket}:run:2", ticket)
    )
    approved = record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="approved"
    )
    assert approved != rejected
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM approvals WHERE action_type='send_reply' AND action_ref=%s",
        (ticket,),
    ).fetchone()["n"] == 2
    # a replay inside the new run stays idempotent
    assert record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="approved"
    ) == approved


def test_decision_replay_with_a_new_key_does_not_409(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = _create_and_pause(db_pool, migrated_conn, two_customers)
    first = decide(
        "send_reply", ticket, DecisionBody(decision="approve"), None, db_pool, "mia",
        f"itst-replay-a-{ticket}",
    )
    _run_until(
        db_pool, migrated_conn, ticket,
        "SELECT count(*) AS n FROM outbox WHERE ticket_id=%s",
    )
    # same decision, different Idempotency-Key, resume job already done
    second = decide(
        "send_reply", ticket, DecisionBody(decision="approve"), None, db_pool, "mia",
        f"itst-replay-b-{ticket}",
    )
    assert second.approval_id == first.approval_id
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM outbox WHERE ticket_id=%s", (ticket,)
    ).fetchone()["n"] == 1
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE ticket_id=%s AND kind='resume_agent'", (ticket,)
    ).fetchone()["n"] == 1
