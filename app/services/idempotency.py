"""Concurrency-safe HTTP idempotency claims and response replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode


def request_hash(body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def execute_once(
    pool: Any,
    *,
    key: str,
    endpoint: str,
    body: Any,
    handler: Callable[[], tuple[int, Any]],
) -> tuple[int, Any, bool]:
    if not key.strip():
        raise ToolFailure(ToolErrorCode.INVALID_INPUT, "Idempotency-Key is required")
    digest = request_hash(body)
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "INSERT INTO idempotency_keys (key, endpoint, request_hash, expires_at) "
            "VALUES (%s,%s,%s,now() + make_interval(secs => %s)) "
            "ON CONFLICT (key, endpoint) DO NOTHING RETURNING key",
            (key, endpoint, digest, 86400),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT request_hash, response_status, response_body, created_at "
                "FROM idempotency_keys WHERE key = %s AND endpoint = %s FOR UPDATE",
                (key, endpoint),
            ).fetchone()
            if existing["request_hash"] != digest:
                raise ToolFailure(ToolErrorCode.INVALID_INPUT, "idempotency key payload mismatch")
            if existing["response_status"] is not None:
                return int(existing["response_status"]), existing["response_body"], True
            age = (datetime.now(UTC) - existing["created_at"]).total_seconds()
            if age < settings.idempotency_stale_after_s:
                raise ToolFailure(ToolErrorCode.DUPLICATE, "request is already in progress", True)
            conn.execute(
                "UPDATE idempotency_keys SET created_at = now(), response_status = NULL, "
                "response_body = NULL WHERE key = %s AND endpoint = %s",
                (key, endpoint),
            )
    try:
        status, response = handler()
    except Exception:
        with pool.connection() as conn:
            conn.execute(
                "DELETE FROM idempotency_keys WHERE key = %s AND endpoint = %s "
                "AND response_status IS NULL", (key, endpoint)
            )
        raise
    with pool.connection() as conn:
        conn.execute(
            "UPDATE idempotency_keys SET response_status = %s, response_body = %s "
            "WHERE key = %s AND endpoint = %s",
            (status, json.dumps(response, default=str), key, endpoint),
        )
    return status, response, False


def cleanup(pool: Any) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "DELETE FROM idempotency_keys WHERE expires_at < now() RETURNING key"
        ).fetchall()
    return len(row)
