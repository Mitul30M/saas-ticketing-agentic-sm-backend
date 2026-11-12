"""P3-2: approval recording and execution semantics against Postgres.

One final decision per ticket/action_ref; a flipped decision is a DUPLICATE.
The executor derives the sent text from the DB (edited_text, else
tickets.draft_reply) and is replay-safe with exactly one audit row."""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from app.services.approvals import record_reply_decision
from app.services.executor import execute_approved_reply
from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode

DRAFT = "grounded draft about the refund policy [KB-006]"
EDITED = "human edited reply [KB-006]"


def _service_ticket(
    migrated_conn: Any, two_customers: Any, draft_text: str | None = None
) -> str:
    ticket = migrated_conn.execute(
        "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (
            two_customers.alice_id,
            f"ITST service {uuid.uuid4().hex[:8]}",
            "How does the refund policy work?",
            f"itst-service-{uuid.uuid4().hex}",
            two_customers.alice_email,
        ),
    ).fetchone()["id"]
    if draft_text is not None:
        migrated_conn.execute(
            "UPDATE tickets SET draft_reply = %s WHERE id = %s", (draft_text, ticket)
        )
    return str(ticket)


def _decision_count(migrated_conn: Any, ticket_id: str) -> int:
    return migrated_conn.execute(
        "SELECT count(*) AS n FROM approvals WHERE action_type='send_reply' "
        "AND action_ref = %s AND decision IN ('approved','rejected')",
        (ticket_id,),
    ).fetchone()["n"]


def _audit_count(migrated_conn: Any, ticket_id: str, event: str) -> int:
    return migrated_conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE entity_id = %s AND event = %s",
        (ticket_id, event),
    ).fetchone()["n"]


def _outbox_count(migrated_conn: Any, ticket_id: str) -> int:
    return migrated_conn.execute(
        "SELECT count(*) AS n FROM outbox WHERE ticket_id = %s", (ticket_id,)
    ).fetchone()["n"]


# --- record_reply_decision -------------------------------------------------


def test_same_decision_twice_is_idempotent(
    migrated_conn: Any, two_customers: Any, db_pool: Any
) -> None:
    ticket = _service_ticket(migrated_conn, two_customers)
    first = record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="approved"
    )
    second = record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="approved"
    )
    assert first == second
    assert _decision_count(migrated_conn, ticket) == 1
    assert _audit_count(migrated_conn, ticket, "reply_decision_recorded") == 1


def test_flip_to_rejected_is_duplicate(migrated_conn: Any, two_customers: Any, db_pool: Any) -> None:
    ticket = _service_ticket(migrated_conn, two_customers)
    record_reply_decision(db_pool, ticket_id=ticket, decided_by="mia", decision="approved")
    with pytest.raises(ToolFailure) as exc:
        record_reply_decision(db_pool, ticket_id=ticket, decided_by="sam", decision="rejected")
    assert exc.value.code == ToolErrorCode.DUPLICATE
    assert _decision_count(migrated_conn, ticket) == 1
    row = migrated_conn.execute(
        "SELECT decision, decided_by FROM approvals WHERE action_type='send_reply' "
        "AND action_ref = %s",
        (ticket,),
    ).fetchone()
    assert row["decision"] == "approved"
    assert row["decided_by"] == "mia"


def test_reject_then_approve_is_duplicate(migrated_conn: Any, two_customers: Any, db_pool: Any) -> None:
    ticket = _service_ticket(migrated_conn, two_customers)
    record_reply_decision(db_pool, ticket_id=ticket, decided_by="mia", decision="rejected")
    with pytest.raises(ToolFailure) as exc:
        record_reply_decision(db_pool, ticket_id=ticket, decided_by="sam", decision="approved")
    assert exc.value.code == ToolErrorCode.DUPLICATE
    assert _decision_count(migrated_conn, ticket) == 1


def test_invalid_inputs(migrated_conn: Any, two_customers: Any, db_pool: Any) -> None:
    ticket = _service_ticket(migrated_conn, two_customers)
    with pytest.raises(ToolFailure) as exc:
        record_reply_decision(db_pool, ticket_id=ticket, decided_by="mia", decision="maybe")
    assert exc.value.code == ToolErrorCode.INVALID_INPUT
    with pytest.raises(ToolFailure) as exc:
        record_reply_decision(db_pool, ticket_id=ticket, decided_by="   ", decision="approved")
    assert exc.value.code == ToolErrorCode.INVALID_INPUT
    with pytest.raises(ToolFailure) as exc:
        record_reply_decision(
            db_pool, ticket_id=str(uuid.uuid4()), decided_by="mia", decision="approved"
        )
    assert exc.value.code == ToolErrorCode.NOT_FOUND


def test_concurrent_approvals_record_exactly_one_row(
    migrated_conn: Any, two_customers: Any, db_pool: Any
) -> None:
    ticket = _service_ticket(migrated_conn, two_customers)
    barrier = threading.Barrier(2)
    results: list[str] = []
    failures: list[Exception] = []

    def worker(name: str) -> None:
        barrier.wait()
        try:
            results.append(
                record_reply_decision(db_pool, ticket_id=ticket, decided_by=name,
                                      decision="approved")
            )
        except Exception as exc:  # collected on purpose; asserted below
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures, failures
    assert len(set(results)) == 1, "both threads must see the same approval id"
    assert _decision_count(migrated_conn, ticket) == 1
    assert _audit_count(migrated_conn, ticket, "reply_decision_recorded") == 1


# --- execute_approved_reply ------------------------------------------------


def test_executor_rejects_unauthorized_calls(
    migrated_conn: Any, two_customers: Any, db_pool: Any
) -> None:
    ticket = _service_ticket(migrated_conn, two_customers, draft_text=DRAFT)
    approval_id = record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="approved"
    )
    other_ticket = _service_ticket(migrated_conn, two_customers, draft_text=DRAFT)

    def _expect_failure(**kwargs: Any) -> None:
        with pytest.raises(ToolFailure):
            execute_approved_reply(db_pool, **kwargs)
        assert _outbox_count(migrated_conn, ticket) == 0

    _expect_failure(ticket_id=ticket, approval_id=approval_id, approved_by="not-mia")
    _expect_failure(ticket_id=other_ticket, approval_id=approval_id, approved_by="mia")
    _expect_failure(ticket_id=other_ticket, approval_id=str(uuid.uuid4()), approved_by="mia")


def test_executor_rejects_a_rejected_decision(
    migrated_conn: Any, two_customers: Any, db_pool: Any
) -> None:
    ticket = _service_ticket(migrated_conn, two_customers, draft_text=DRAFT)
    approval_id = record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="rejected"
    )
    with pytest.raises(ToolFailure):
        execute_approved_reply(
            db_pool, ticket_id=ticket, approval_id=approval_id, approved_by="mia"
        )
    assert _outbox_count(migrated_conn, ticket) == 0


def test_executor_replay_is_idempotent(
    migrated_conn: Any, two_customers: Any, db_pool: Any
) -> None:
    ticket = _service_ticket(migrated_conn, two_customers, draft_text=DRAFT)
    approval_id = record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="approved"
    )
    first = execute_approved_reply(
        db_pool, ticket_id=ticket, approval_id=approval_id, approved_by="mia"
    )
    second = execute_approved_reply(
        db_pool, ticket_id=ticket, approval_id=approval_id, approved_by="mia"
    )
    assert first == second
    assert _outbox_count(migrated_conn, ticket) == 1
    assert _audit_count(migrated_conn, ticket, "reply_executed") == 1
    row = migrated_conn.execute(
        "SELECT body FROM outbox WHERE ticket_id = %s", (ticket,)
    ).fetchone()
    assert row["body"] == DRAFT  # original draft when the human did not edit


def test_executor_sends_edited_text_when_the_human_edited(
    migrated_conn: Any, two_customers: Any, db_pool: Any
) -> None:
    ticket = _service_ticket(migrated_conn, two_customers, draft_text=DRAFT)
    approval_id = record_reply_decision(
        db_pool, ticket_id=ticket, decided_by="mia", decision="approved",
        edited_text=EDITED,
    )
    execute_approved_reply(
        db_pool, ticket_id=ticket, approval_id=approval_id, approved_by="mia"
    )
    row = migrated_conn.execute(
        "SELECT body FROM outbox WHERE ticket_id = %s", (ticket,)
    ).fetchone()
    assert row["body"] == EDITED
