"""Job-queue semantics: dedupe, claiming, stale locks, terminal failure."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.config import settings
from app.services import jobs
from app.services.ticket_state import transition_ticket
from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode

pytestmark = pytest.mark.integration


def _ticket(conn: Any, customer: Any, status: str = "new") -> str:
    return str(
        conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, status) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                customer.alice_id,
                f"ITST job {uuid.uuid4().hex[:8]}",
                "job queue test",
                f"itst-job-{uuid.uuid4().hex}",
                customer.alice_email,
                status,
            ),
        ).fetchone()["id"]
    )


def _claim_ours(pool: Any, job_id: str, worker: str = "itst-worker") -> dict[str, Any]:
    """Claim until our job is picked; unrelated leftovers are finished first."""
    for _ in range(20):
        job = jobs.claim(pool, worker)
        assert job is not None, "our job was never claimable"
        if str(job["id"]) == job_id:
            return job
        jobs.complete(pool, str(job["id"]))
    raise AssertionError("job not claimed")


def _job(conn: Any, job_id: str) -> dict[str, Any]:
    return conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()


def test_double_enqueue_is_one_job_even_after_completion(
    db_pool: Any, two_customers: Any
) -> None:
    ticket = two_customers.ticket_id
    key = f"run_agent:{ticket}:1"
    first = jobs.enqueue(db_pool, ticket, "run_agent", {}, key)
    second = jobs.enqueue(db_pool, ticket, "run_agent", {}, key)
    assert first == second
    claimed = _claim_ours(db_pool, first)
    assert int(claimed["attempts"]) == 1
    jobs.complete(db_pool, first)
    # a completed dedupe key still replays the same job instead of inserting
    assert jobs.enqueue(db_pool, ticket, "run_agent", {}, key) == first


def test_enqueue_resets_a_failed_job(db_pool: Any, migrated_conn: Any, two_customers: Any) -> None:
    ticket = two_customers.ticket_id
    key = f"resume:{ticket}:dead"
    job_id = jobs.enqueue(db_pool, ticket, "resume_agent", {"approved": True}, key)
    claimed = _claim_ours(db_pool, job_id)
    jobs.fail(db_pool, {**claimed, "attempts": settings.job_max_attempts}, "boom")
    assert _job(migrated_conn, job_id)["status"] == "failed"

    again = jobs.enqueue(db_pool, ticket, "resume_agent", {"approved": True}, key)
    assert again == job_id
    row = _job(migrated_conn, job_id)
    assert row["status"] == "queued"
    assert int(row["attempts"]) == 0
    assert row["last_error"] is None
    jobs.complete(db_pool, _claim_ours(db_pool, job_id)["id"])


def test_claim_reclaims_a_stale_lock(db_pool: Any, migrated_conn: Any, two_customers: Any) -> None:
    ticket = two_customers.ticket_id
    job_id = jobs.enqueue(db_pool, ticket, "run_agent", {}, f"run_agent:{ticket}:1")
    first = _claim_ours(db_pool, job_id)
    assert int(first["attempts"]) == 1
    migrated_conn.execute(
        "UPDATE jobs SET locked_at = now() - make_interval(secs => %s) WHERE id=%s",
        (settings.job_lock_timeout_s + 60, job_id),
    )
    reclaimed = _claim_ours(db_pool, job_id, "itst-worker-2")
    assert int(reclaimed["attempts"]) == 2
    assert reclaimed["locked_by"] == "itst-worker-2"
    jobs.complete(db_pool, job_id)


def test_claim_terminates_a_stale_job_at_max_attempts(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = two_customers.ticket_id
    migrated_conn.execute("UPDATE tickets SET status='triaging' WHERE id=%s", (ticket,))
    job_id = jobs.enqueue(db_pool, ticket, "run_agent", {}, f"run_agent:{ticket}:1")
    _claim_ours(db_pool, job_id)
    migrated_conn.execute(
        "UPDATE jobs SET attempts=%s, locked_at = now() - make_interval(secs => %s) WHERE id=%s",
        (settings.job_max_attempts, settings.job_lock_timeout_s + 60, job_id),
    )
    # the row is reclaimed and terminated instead of running forever
    while True:
        other = jobs.claim(db_pool, "itst-worker")
        if other is None:
            break
        jobs.complete(db_pool, str(other["id"]))
    row = _job(migrated_conn, job_id)
    assert row["status"] == "failed"
    assert "final attempt" in row["last_error"]
    ticket_row = migrated_conn.execute(
        "SELECT status, escalated, escalation_reason, agent_outcome FROM tickets WHERE id=%s",
        (ticket,),
    ).fetchone()
    assert ticket_row == {
        "status": "awaiting_human",
        "escalated": True,
        "escalation_reason": "JOB_FAILED",
        "agent_outcome": "failed",
    }
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE event='job_failed' AND entity_id=%s",
        (ticket,),
    ).fetchone()["n"] == 1


def test_terminal_failure_parks_the_ticket_and_audits(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = two_customers.ticket_id
    migrated_conn.execute("UPDATE tickets SET status='triaging' WHERE id=%s", (ticket,))
    job_id = jobs.enqueue(db_pool, ticket, "run_agent", {}, f"run_agent:{ticket}:1")
    claimed = _claim_ours(db_pool, job_id)
    jobs.fail(db_pool, {**claimed, "attempts": settings.job_max_attempts}, "database unavailable")

    row = _job(migrated_conn, job_id)
    assert row["status"] == "failed"
    assert row["last_error"] == "database unavailable"
    assert migrated_conn.execute(
        "SELECT status, escalation_reason FROM tickets WHERE id=%s", (ticket,)
    ).fetchone() == {"status": "awaiting_human", "escalation_reason": "JOB_FAILED"}
    audit = migrated_conn.execute(
        "SELECT payload FROM audit_log WHERE event='job_failed' AND entity_id=%s", (ticket,)
    ).fetchall()
    assert len(audit) == 1
    assert audit[0]["payload"]["job_id"] == job_id


def test_rerun_refuses_active_runs_and_tickets_awaiting_approval(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = two_customers.ticket_id
    job_id, run_number = jobs.enqueue_rerun(db_pool, ticket)
    assert run_number == 1
    assert migrated_conn.execute(
        "SELECT status, thread_id FROM tickets WHERE id=%s", (ticket,)
    ).fetchone() == {"status": "triaging", "thread_id": f"ticket:{ticket}:run:1"}
    with pytest.raises(ToolFailure) as exc:
        jobs.enqueue_rerun(db_pool, ticket)
    assert exc.value.code == ToolErrorCode.DUPLICATE

    jobs.complete(db_pool, job_id)
    migrated_conn.execute("UPDATE tickets SET status='awaiting_approval' WHERE id=%s", (ticket,))
    with pytest.raises(ToolFailure) as exc:
        jobs.enqueue_rerun(db_pool, ticket)
    assert exc.value.code == ToolErrorCode.DUPLICATE


def test_rerun_reopens_a_resolved_ticket_through_the_guard(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = two_customers.ticket_id
    migrated_conn.execute("UPDATE tickets SET status='resolved' WHERE id=%s", (ticket,))
    job_id, run_number = jobs.enqueue_rerun(db_pool, ticket)
    row = migrated_conn.execute(
        "SELECT status, thread_id FROM tickets WHERE id=%s", (ticket,)
    ).fetchone()
    assert row["status"] == "triaging"
    assert row["thread_id"] == f"ticket:{ticket}:run:{run_number}"
    assert _job(migrated_conn, job_id)["kind"] == "run_agent"
    jobs.complete(db_pool, _claim_ours(db_pool, job_id)["id"])


def test_transition_guard_rejects_illegal_moves(
    db_pool: Any, migrated_conn: Any, two_customers: Any
) -> None:
    ticket = two_customers.ticket_id
    with migrated_conn.transaction():
        with pytest.raises(ToolFailure) as exc:
            transition_ticket(migrated_conn, ticket, "awaiting_approval")
        assert exc.value.code == ToolErrorCode.BUSINESS_RULE
    migrated_conn.execute("UPDATE tickets SET status='resolved' WHERE id=%s", (ticket,))
    with migrated_conn.transaction():
        with pytest.raises(ToolFailure):
            transition_ticket(migrated_conn, ticket, "awaiting_approval")
        transition_ticket(migrated_conn, ticket, "triaging")
    assert migrated_conn.execute(
        "SELECT status FROM tickets WHERE id=%s", (ticket,)
    ).fetchone()["status"] == "triaging"
