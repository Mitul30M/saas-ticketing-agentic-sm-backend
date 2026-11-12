"""Postgres-backed job queue primitives."""

from __future__ import annotations

import json
from typing import Any

from app.config import settings


def enqueue(pool: Any, ticket_id: str, kind: str, payload: dict[str, Any], dedupe_key: str) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO jobs (ticket_id, kind, payload, dedupe_key, max_attempts) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key) DO UPDATE SET dedupe_key = EXCLUDED.dedupe_key "
            "RETURNING id",
            (ticket_id, kind, json.dumps(payload), dedupe_key, settings.job_max_attempts),
        ).fetchone()
    return str(row["id"])


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
        updated = conn.execute(
            "UPDATE jobs SET status='running', locked_at=now(), locked_by=%s, "
            "attempts=attempts+1, updated_at=now() WHERE id=%s RETURNING *",
            (worker_id, row["id"]),
        ).fetchone()
    return dict(updated) if updated is not None else None


def complete(pool: Any, job_id: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', locked_at=NULL, locked_by=NULL, updated_at=now() "
            "WHERE id=%s AND status='running'", (job_id,)
        )


def fail(pool: Any, job: dict[str, Any], error: str) -> None:
    terminal = int(job["attempts"]) >= int(job["max_attempts"])
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status=%s, last_error=%s, run_after=now() + "
            "make_interval(secs => %s), locked_at=NULL, locked_by=NULL, updated_at=now() "
            "WHERE id=%s AND status='running'",
            ("failed" if terminal else "queued", error[:2000], 2 ** int(job["attempts"]), job["id"]),
        )
