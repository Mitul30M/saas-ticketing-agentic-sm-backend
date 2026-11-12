"""Idempotency-key semantics: replay, mismatch, concurrency, claim release."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest

from app.services.idempotency import cleanup, execute_once, request_hash
from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode

pytestmark = pytest.mark.integration


def _key() -> str:
    return f"itst-idem-{uuid.uuid4().hex}"


def test_missing_key_is_rejected(db_pool: Any) -> None:
    with pytest.raises(ToolFailure) as exc:
        execute_once(db_pool, key="  ", endpoint="POST:test", body={}, handler=lambda: (200, {}))
    assert exc.value.code == ToolErrorCode.INVALID_INPUT


def test_same_key_and_body_replays_the_stored_response(db_pool: Any) -> None:
    calls: list[int] = []
    key = _key()

    def handler() -> tuple[int, Any]:
        calls.append(1)
        return 201, {"ticket_id": "t-1"}

    first = execute_once(db_pool, key=key, endpoint="POST:test", body={"a": 1}, handler=handler)
    second = execute_once(db_pool, key=key, endpoint="POST:test", body={"a": 1}, handler=handler)
    assert first == (201, {"ticket_id": "t-1"}, False)
    assert second == (201, {"ticket_id": "t-1"}, True)
    assert len(calls) == 1


def test_same_key_with_a_different_body_is_rejected(db_pool: Any) -> None:
    key = _key()
    execute_once(db_pool, key=key, endpoint="POST:test", body={"a": 1}, handler=lambda: (200, {}))
    with pytest.raises(ToolFailure) as exc:
        execute_once(db_pool, key=key, endpoint="POST:test", body={"a": 2}, handler=lambda: (200, {}))
    assert exc.value.code == ToolErrorCode.INVALID_INPUT


def test_handler_failure_releases_the_claim_for_a_retry(db_pool: Any) -> None:
    key = _key()

    def boom() -> tuple[int, Any]:
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError):
        execute_once(db_pool, key=key, endpoint="POST:test", body={"a": 1}, handler=boom)
    result = execute_once(
        db_pool, key=key, endpoint="POST:test", body={"a": 1}, handler=lambda: (201, {"ok": True})
    )
    assert result == (201, {"ok": True}, False)


def test_concurrent_requests_run_the_handler_once(db_pool: Any) -> None:
    key = _key()
    barrier = threading.Barrier(2)
    calls: list[int] = []
    results: list[tuple[int, Any, bool]] = []
    failures: list[ToolFailure] = []

    def handler() -> tuple[int, Any]:
        calls.append(1)
        time.sleep(0.5)  # keep the claim in flight so the peer sees it
        return 201, {"ok": True}

    def worker() -> None:
        barrier.wait()
        try:
            results.append(
                execute_once(
                    db_pool, key=key, endpoint="POST:test", body={"a": 1}, handler=handler
                )
            )
        except ToolFailure as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert len(results) == 1
    assert len(failures) == 1
    assert failures[0].code == ToolErrorCode.DUPLICATE
    assert failures[0].retryable is True


def test_expired_in_flight_claim_is_reclaimed(
    db_pool: Any, migrated_conn: Any
) -> None:
    key = _key()
    digest = request_hash({"a": 1})
    migrated_conn.execute(
        "INSERT INTO idempotency_keys (key, endpoint, request_hash, created_at) "
        "VALUES (%s,'POST:test',%s, now() - interval '2 hours')",
        (key, digest),
    )
    result = execute_once(
        db_pool, key=key, endpoint="POST:test", body={"a": 1}, handler=lambda: (201, {"ok": True})
    )
    assert result == (201, {"ok": True}, False)


def test_cleanup_removes_only_expired_keys(db_pool: Any, migrated_conn: Any) -> None:
    expired = _key()
    current = _key()
    migrated_conn.execute(
        "INSERT INTO idempotency_keys (key, endpoint, request_hash, expires_at) "
        "VALUES (%s,'POST:test','x', now() - interval '1 hour'), "
        "(%s,'POST:test','y', now() + interval '1 hour')",
        (expired, current),
    )
    cleanup(db_pool)
    remaining = migrated_conn.execute(
        "SELECT key FROM idempotency_keys WHERE key IN (%s,%s)", (expired, current)
    ).fetchall()
    assert [row["key"] for row in remaining] == [current]
