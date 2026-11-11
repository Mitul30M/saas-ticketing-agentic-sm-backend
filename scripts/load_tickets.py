"""Load linked tickets from data/tickets_all.jsonl into the DB (PLAN 4.2).

Follows the migration-006 intake semantics:
- from_email/from_name are the TRANSPORT sender (taken from the jsonl, which
  the linker generated from the assigned customer's account email).
- customer_id is resolved from the curated `external_ref`; runtime tools must
  re-verify identity from from_email (duplicate-email edge stays flagged).
- source = 'import', status = 'new'.

Idempotent: a rerun skips rows whose (from_email, content_hash) already exist
(the tickets table no longer has a global unique hash — dedupe is soft and
windowed in the live intake service).

    uv run python -m scripts.load_tickets
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.db import connect

TICKETS_PATH = Path("data/tickets_all.jsonl")


def load_tickets(path: Path = TICKETS_PATH) -> dict[str, int]:
    if not path.exists():
        raise RuntimeError(f"tickets file missing: {path} — run scripts.link_tickets first")
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    inserted = skipped_existing = unknown_sender = 0
    now = datetime.now(UTC)
    with connect(settings.database_url) as conn:
        for rec in records:
            from_email = rec.get("from_email")
            customer_id: str | None = None
            external_ref = rec.get("external_ref")
            if external_ref:
                row = conn.execute(
                    "SELECT id FROM customers WHERE external_ref = %s", (external_ref,)
                ).fetchone()
                if row:
                    customer_id = str(row["id"])
                else:
                    unknown_sender += 1
            content_hash = hashlib.sha256(rec["body"].encode()).hexdigest()
            received_at = now - timedelta(hours=float(rec.get("received_hours_ago", 24)))
            existing = conn.execute(
                """
                SELECT 1 FROM tickets
                WHERE from_email IS NOT DISTINCT FROM %s AND content_hash = %s
                LIMIT 1
                """,
                (from_email, content_hash),
            ).fetchone()
            if existing:
                skipped_existing += 1
                continue
            conn.execute(
                """
                INSERT INTO tickets
                    (customer_id, channel, subject, body, content_hash, received_at, status,
                     from_email, from_name, source)
                VALUES (%s, %s, %s, %s, %s, %s, 'new', %s, %s, 'import')
                """,
                (customer_id, rec.get("channel") or "email", rec["subject"], rec["body"],
                 content_hash, received_at, from_email, rec.get("from_name")),
            )
            inserted += 1
    stats = {
        "records": len(records),
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "unknown_sender": unknown_sender,
    }
    print("load_tickets:", stats)
    return stats


if __name__ == "__main__":
    load_tickets()
