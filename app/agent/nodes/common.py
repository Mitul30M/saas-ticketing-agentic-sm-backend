"""Shared node plumbing: escalation updates, the step guard, and the safety
wrapper that turns any unexpected node failure into a fail-closed escalation
(invariant 7: every failure ends in a safe state, never a crash)."""

from __future__ import annotations

import contextlib
import traceback
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.graph._node import _Node

from app import db
from app.agent.state import AgentState
from app.config import settings


def trace_node(
    pool: Any,
    ticket_id: str,
    trace_id: str,
    node: str,
    kind: str,
    input_: dict[str, Any] | None = None,
    output: Any = None,
    *,
    status: str = "ok",
    error: str | None = None,
    duration_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cache_hit: bool | None = None,
) -> None:
    """One trace row for a node execution (see db.trace)."""
    with pool.connection() as conn:
        db.trace(
            conn, ticket_id, trace_id, node, kind, input_, output,
            status=status, error=error, duration_ms=duration_ms,
            tokens_in=tokens_in, tokens_out=tokens_out, cache_hit=cache_hit,
        )


def escalation_update(
    reason_code: str, detail: str, queue: str | None = None
) -> dict[str, Any]:
    """State update that routes the run to the escalate node."""
    escalation: dict[str, Any] = {
        "reason_code": reason_code,
        "detail": detail[:2000] or reason_code,
    }
    if queue:
        escalation["queue"] = queue
    return {"outcome": "escalated", "escalation": escalation}


def step_guard(state: AgentState) -> dict[str, Any] | None:
    """Fail-closed STEP_LIMIT escalation once the run exhausts its budget.

    Checked at the top of every LLM/tool node; also backed by the explicit
    recursion_limit passed at invoke time (graph.py)."""
    if int(state.get("step_count") or 0) >= settings.step_limit:
        return escalation_update(
            "STEP_LIMIT",
            f"step budget of {settings.step_limit} exhausted before '{state.get('outcome', '')}'",
        )
    return None


def next_step(state: AgentState) -> int:
    return int(state.get("step_count") or 0) + 1


def safe_node(name: str, pool: Any, fn: Callable[[AgentState], dict[str, Any]]) -> _Node[AgentState]:
    """Wrap a node so ANY unexpected exception becomes a fail-closed escalation
    with a traced error row. Nodes raise nothing into the graph."""

    def wrapped(state: AgentState) -> dict[str, Any]:
        ticket_id = str(state.get("ticket_id") or "")
        trace_id = str(state.get("trace_id") or "")
        try:
            return fn(state)
        except GraphBubbleUp:
            # GraphInterrupt (and friends) are the graph's control flow — the
            # approval pause must propagate, never become a "failure".
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):  # tracing must never mask the failure
                trace_node(
                    pool, ticket_id, trace_id, name, "error",
                    None, None, status="error", error=type(exc).__name__,
                )
            return {
                "outcome": "escalated",
                "escalation": {
                    "reason_code": "INTERNAL_ERROR",
                    "detail": f"node {name} failed; escalated to a human (no internals leaked)",
                },
                "errors": [f"{name}: {type(exc).__name__}: {exc}"],
            }

    return wrapped


def format_exception(exc: Exception) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()
