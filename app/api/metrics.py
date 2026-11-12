"""Dashboard aggregate metrics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.dependencies import Pool, Protected

router = APIRouter(prefix="/api/metrics")


@router.get("/summary")
def summary(_: Protected, pool: Pool) -> dict[str, Any]:
    with pool.connection() as conn:
        statuses = conn.execute(
            "SELECT status, count(*) AS count FROM tickets GROUP BY status ORDER BY status"
        ).fetchall()
        outcomes = conn.execute(
            "SELECT COALESCE(agent_outcome,'pending') AS outcome, count(*) AS count "
            "FROM tickets GROUP BY COALESCE(agent_outcome,'pending') ORDER BY outcome"
        ).fetchall()
        trace = conn.execute(
            "SELECT count(*) AS events, COALESCE(sum(tokens_in),0) AS tokens_in, "
            "COALESCE(sum(tokens_out),0) AS tokens_out, COALESCE(sum(cost_usd),0) AS cost_usd, "
            "COALESCE(avg(duration_ms),0) AS avg_latency_ms, "
            "COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms),0) AS p95_latency_ms "
            "FROM trace_events"
        ).fetchone()
        jobs = conn.execute(
            "SELECT status, count(*) AS count FROM jobs GROUP BY status ORDER BY status"
        ).fetchall()
    return {
        "tickets_by_status": [dict(row) for row in statuses],
        "outcomes": [dict(row) for row in outcomes],
        "trace": dict(trace),
        "jobs": [dict(row) for row in jobs],
    }
