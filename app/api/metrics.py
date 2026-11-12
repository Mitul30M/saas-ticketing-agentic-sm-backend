"""Dashboard aggregate metrics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.dependencies import Pool, Protected
from app.services.tracing import pricing

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
        categories = conn.execute(
            "SELECT COALESCE(category,'unclassified') AS category, count(*) AS count "
            "FROM tickets GROUP BY 1 ORDER BY 1"
        ).fetchall()
        queues = conn.execute(
            "SELECT COALESCE(queue,'unassigned') AS queue, count(*) AS count "
            "FROM tickets GROUP BY 1 ORDER BY 1"
        ).fetchall()
        escalations = conn.execute(
            "SELECT COALESCE(escalation_reason,'none') AS reason, count(*) AS count "
            "FROM tickets GROUP BY 1 ORDER BY 1"
        ).fetchall()
        nodes = conn.execute(
            "SELECT node, count(*) AS events, COALESCE(avg(duration_ms),0) AS avg_latency_ms, "
            "COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms),0) AS p95_latency_ms "
            "FROM trace_events GROUP BY node ORDER BY node"
        ).fetchall()
        tool_errors = conn.execute(
            "SELECT count(*) FILTER (WHERE kind='tool' AND status='error') AS errors, "
            "count(*) FILTER (WHERE kind='tool') AS calls FROM trace_events"
        ).fetchone()
        cache = conn.execute(
            "SELECT count(*) FILTER (WHERE cache_hit) AS hits, "
            "count(*) FILTER (WHERE kind='llm') AS calls FROM trace_events"
        ).fetchone()
    reference = pricing().get("reference", {})
    shadow_cost = (
        float(trace["tokens_in"]) * float(reference.get("input_per_million", 0)) / 1_000_000
        + float(trace["tokens_out"]) * float(reference.get("output_per_million", 0)) / 1_000_000
    )
    return {
        "tickets_by_status": [dict(row) for row in statuses],
        "outcomes": [dict(row) for row in outcomes],
        "trace": {
            **dict(trace),
            "actual_cost_usd": float(trace["cost_usd"]),
            "shadow_cost_usd": shadow_cost,
            "llm_cache_hit_rate": (
                float(cache["hits"]) / float(cache["calls"]) if cache["calls"] else 0.0
            ),
            "tool_error_rate": (
                float(tool_errors["errors"]) / float(tool_errors["calls"])
                if tool_errors["calls"] else 0.0
            ),
        },
        "tickets_by_category": [dict(row) for row in categories],
        "tickets_by_queue": [dict(row) for row in queues],
        "escalations_by_reason": [dict(row) for row in escalations],
        "node_latency": [dict(row) for row in nodes],
        "jobs": [dict(row) for row in jobs],
    }
