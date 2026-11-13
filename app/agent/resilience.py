"""Bounded retries for LLM calls (invariant 7): exponential backoff + jitter,
at most settings.llm_max_calls attempts, and one trace row per FAILED attempt
so the audit shows what was retried. The successful call is traced by the
node itself."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tenacity import RetryCallState, retry, retry_if_exception_type, wait_exponential_jitter
from tenacity.stop import stop_after_attempt

from app import db
from app.config import settings
from app.llm.base import LLMError


def call_llm[T](
    fn: Callable[[], T], *, pool: Any, ticket_id: str, trace_id: str, node: str
) -> T:
    """Run one LLM-producing callable with bounded retries on LLMError."""

    def _log_failed_attempt(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None and outcome.failed else None
        with pool.connection() as conn:
            db.trace(
                conn,
                ticket_id,
                trace_id,
                node,
                "llm_retry",
                {"attempt": retry_state.attempt_number},
                None,
                status="error",
                error=(f"{type(exc).__name__}: {exc}"[:300] if exc is not None else "unknown"),
            )

    wrapper = retry(
        retry=retry_if_exception_type(LLMError),
        stop=stop_after_attempt(max(1, settings.llm_max_calls)),
        wait=wait_exponential_jitter(initial=1, max=3),
        before_sleep=_log_failed_attempt,
        reraise=True,
    )
    retried = wrapper(fn)
    return retried()
