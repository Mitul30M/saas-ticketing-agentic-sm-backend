"""Postgres-backed job queue primitives.

Claiming uses `FOR UPDATE SKIP LOCKED`; a running job whose worker died is
reclaimed after `job_lock_timeout_s`. A job that died on its final attempt is
failed terminally (the ticket is parked for a human) instead of sitting
`running` forever.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Json

from app.config import settings
from app.services.ticket_state import transition_ticket
from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode


def enqueue(pool: Any, ticket_id: str, kind: str, payload: dict[str, Any], dedupe_key: str) -> str:
    """Enqueue one job; a repeated dedupe key returns the existing job id.

    - live (queued/running) or already done -> the existing id (replays are
      no-ops, so a decision replay cannot turn into a 409 after the resume job
      finished);
    - previously failed -> reset to queued so a retried request can progress.
    """
    with pool.connection() as conn, conn.transaction():
        live = conn.execute(
            "SELECT id FROM jobs WHERE dedupe_key=%s AND status IN ('queued','running') "
            "FOR UPDATE",
            (dedupe_key,),
        ).fetchone()
        if live is not None:
            return str(live["id"])
        row = conn.execute(
            "INSERT INTO jobs (ticket_id, kind, payload, dedupe_key, max_attempts) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING RETURNING id",
            (ticket_id, kind, json.dumps(payload), dedupe_key, settings.job_max_attempts),
        ).fetchone()
        if row is not None:
            return str(row["id"])
        existing = conn.execute(
            "SELECT id, status FROM jobs WHERE dedupe_key=%s FOR UPDATE", (dedupe_key,)
        ).fetchone()
        if existing is None:
            raise ToolFailure(ToolErrorCode.INTERNAL, "job dedupe key vanished")
        if existing["status"] == "failed":
            conn.execute(
                "UPDATE jobs SET status='queued', attempts=0, last_error=NULL, "
                "run_after=now(), locked_at=NULL, locked_by=NULL, updated_at=now(), "
                "payload=%s WHERE id=%s",
                (json.dumps(payload), existing["id"]),
            )
        return str(existing["id"])


def enqueue_rerun(pool: Any, ticket_id: str) -> tuple[str, int]:
    """Queue a fresh agent run on a new thread; returns (job_id, run_number)."""
    with pool.connection() as conn, conn.transaction():
        ticket = conn.execute(
            "SELECT status FROM tickets WHERE id=%s FOR UPDATE", (ticket_id,)
        ).fetchone()
        if ticket is None:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, "ticket not found")
        active = conn.execute(
            "SELECT 1 FROM jobs WHERE ticket_id=%s AND kind IN ('run_agent','resume_agent') "
            "AND status IN ('queued','running') LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if active is not None or ticket["status"] == "awaiting_approval":
            raise ToolFailure(ToolErrorCode.DUPLICATE, "ticket already has an active run")
        run_number = int(conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE ticket_id=%s AND kind='run_agent'",
            (ticket_id,),
        ).fetchone()["n"]) + 1
        thread_id = f"ticket:{ticket_id}:run:{run_number}"
        # Route through the guarded state machine: a resolved ticket can be
        # re-opened explicitly, but never by a raw UPDATE that skips the guard.
        transition_ticket(conn, ticket_id, "triaging", thread_id=thread_id)
        row = conn.execute(
            "INSERT INTO jobs (ticket_id, kind, payload, dedupe_key, max_attempts) "
            "VALUES (%s,'run_agent',%s,%s,%s) RETURNING id",
            (ticket_id, "{}", f"run_agent:{ticket_id}:{run_number}", settings.job_max_attempts),
        ).fetchone()
    return str(row["id"]), run_number


def _terminate(conn: Any, job: dict[str, Any], error: str) -> None:
    """Fail a job for good and park the ticket for a human."""
    conn.execute(
        "UPDATE jobs SET status='failed', last_error=%s, run_after=now(), "
        "locked_at=NULL, locked_by=NULL, updated_at=now() WHERE id=%s",
        (error[:2000], job["id"]),
    )
    conn.execute(
        "UPDATE tickets SET status='awaiting_human', escalated=true, "
        "escalation_reason='JOB_FAILED', handled_by='unassigned', agent_outcome='failed' "
        "WHERE id=%s AND status <> 'resolved'",
        (job["ticket_id"],),
    )
    conn.execute(
        "INSERT INTO audit_log (actor,event,entity,entity_id,payload) "
        "VALUES ('worker','job_failed','ticket',%s,%s)",
        (str(job["ticket_id"]),
         Json({"job_id": str(job["id"]), "error": error[:500]})),
    )


def claim(pool: Any, worker_id: str) -> dict[str, Any] | None:
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT * FROM jobs WHERE (status='queued' AND run_after <= now()) "
            "OR (status='running' AND locked_at < now() - make_interval(secs => %s)) "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
            (settings.job_lock_timeout_s,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "running" and int(row["attempts"]) >= int(row["max_attempts"]):
            # The previous worker died on the final allowed attempt; reclaiming
            # it would run forever. Fail closed and move on.
            _terminate(conn, dict(row), "worker lock expired after the final attempt")
            return None
        updated = conn.execute(
            "UPDATE jobs SET status='running', locked_at=now(), locked_by=%s, "
            "attempts=attempts+1, updated_at=now() WHERE id=%s RETURNING *",
            (worker_id, row["id"]),
        ).fetchone()
    return dict(updated) if updated is not None else None


def complete(pool: Any, job_id: str) -> None:
    with pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE jobs SET status='done', locked_at=NULL, locked_by=NULL, updated_at=now() "
            "WHERE id=%s AND status='running'", (job_id,)
        )


def fail(pool: Any, job: dict[str, Any], error: str) -> None:
    terminal = int(job["attempts"]) >= int(job["max_attempts"])
    with pool.connection() as conn, conn.transaction():
        if terminal:
            _terminate(conn, job, error)
        else:
            conn.execute(
                "UPDATE jobs SET status='queued', last_error=%s, run_after=now() + "
                "make_interval(secs => %s), locked_at=NULL, locked_by=NULL, updated_at=now() "
                "WHERE id=%s AND status='running'",
                (error[:2000], 2 ** int(job["attempts"]), job["id"]),
            )
