"""Ticket intake and soft, sender-scoped deduplication."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.config import settings
from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_ticket(pool: Any, payload: dict[str, Any]) -> dict[str, Any]:
    subject = str(payload.get("subject") or "").strip()
    body = str(payload.get("body") or "").strip()
    email = str(payload.get("from_email") or "").strip().lower()
    if not subject or len(subject) > 200 or not body or len(body) > 10000:
        raise ToolFailure(ToolErrorCode.INVALID_INPUT, "invalid subject or body length")
    if not EMAIL_RE.match(email):
        raise ToolFailure(ToolErrorCode.INVALID_INPUT, "invalid from_email")
    content_hash = hashlib.sha256(body.encode()).hexdigest()
    with pool.connection() as conn, conn.transaction():
        duplicate = conn.execute(
            "SELECT id FROM tickets WHERE lower(from_email) = %s AND content_hash = %s "
            "AND received_at >= now() - make_interval(mins => %s) "
            "ORDER BY received_at DESC LIMIT 1",
            (email, content_hash, settings.intake_dedupe_window_minutes),
        ).fetchone()
        if duplicate:
            return {"ticket_id": str(duplicate["id"]), "duplicate": True}
        customer = conn.execute(
            "SELECT id FROM customers WHERE lower(email) = %s", (email,)
        ).fetchall()
        customer_id = str(customer[0]["id"]) if len(customer) == 1 else None
        ticket = conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, "
            "from_name, source, status) VALUES (%s,%s,%s,%s,%s,%s,'portal','new') RETURNING id",
            (customer_id, subject, body, content_hash, email, payload.get("from_name")),
        ).fetchone()
        ticket_id = str(ticket["id"])
        conn.execute(
            "UPDATE tickets SET thread_id=%s WHERE id=%s",
            (f"ticket:{ticket_id}:run:1", ticket_id),
        )
        conn.execute(
            "INSERT INTO jobs (ticket_id, kind, payload, dedupe_key) "
            "VALUES (%s,'run_agent',%s,%s) "
            "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING",
            (ticket_id, "{}", f"run_agent:{ticket_id}:1"),
        )
    return {"ticket_id": ticket_id, "duplicate": False}
