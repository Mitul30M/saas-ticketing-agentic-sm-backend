"""Integration tests for kb_search, get_subscription, propose_plan_change,
escalate_to_human (skips without DB). Fixtures in tests/integration/conftest.py."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.tools.registry import build_registry
from app.tools.schemas import ToolContext, ToolErrorCode

pytestmark = pytest.mark.integration


@pytest.fixture()
def registry(db_pool: Any) -> Any:
    return build_registry(db_pool)


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext(trace_id=f"it-{uuid.uuid4().hex[:8]}", timeout_s=5)


def _trace_rows(conn: Any, trace_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT node, kind, status, error FROM trace_events "
        "WHERE trace_id = %s ORDER BY id",
        (trace_id,),
    ).fetchall()


# ---- kb_search -----------------------------------------------------------------

def test_kb_search_returns_ranked_results(registry, two_customers, ctx) -> None:
    result = registry.call(
        "kb_search", {"query": "how do I request a refund", "limit": 3},
        ticket_id=two_customers.ticket_id, ctx=ctx,
    )

    assert result.ok is True
    results = result.data["results"]
    assert 1 <= len(results) <= 3
    first = results[0]
    for key in ("kb_id", "chunk_idx", "title", "text", "rrf_score"):
        assert key in first


def test_kb_search_too_short_query_is_invalid_input(registry, two_customers, ctx) -> None:
    result = registry.call(
        "kb_search", {"query": "ab"}, ticket_id=two_customers.ticket_id, ctx=ctx
    )

    assert result.error.code == ToolErrorCode.INVALID_INPUT


def test_kb_search_writes_one_ok_trace_row(registry, migrated_conn, two_customers, ctx) -> None:
    registry.call(
        "kb_search", {"query": "payment failed", "limit": 2},
        ticket_id=two_customers.ticket_id, ctx=ctx,
    )

    rows = _trace_rows(migrated_conn, ctx.trace_id)
    assert [(r["node"], r["status"]) for r in rows] == [("kb_search", "ok")]


# ---- get_subscription ------------------------------------------------------------

def test_get_subscription_returns_own_subscriptions(registry, two_customers, ctx) -> None:
    t = two_customers
    result = registry.call(
        "get_subscription",
        {"ticket_id": t.ticket_id, "customer_id": str(t.alice_id)},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert result.ok is True
    subs = result.data["subscriptions"]
    assert subs and subs[0]["status"] == "active"


def test_get_subscription_for_claimed_foreign_customer_is_mismatch(registry, two_customers, ctx) -> None:
    t = two_customers
    result = registry.call(
        "get_subscription",
        {"ticket_id": t.ticket_id, "customer_id": str(t.bob_id)},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert result.error.code == ToolErrorCode.OWNERSHIP_MISMATCH


# ---- propose_plan_change ----------------------------------------------------------

def _plan_args(t, to_plan: str = "PLAN-STARTER") -> dict[str, Any]:
    return {
        "ticket_id": t.ticket_id,
        "customer_id": str(t.alice_id),
        "to_plan_id": to_plan,
        "effective": "next_cycle",
        "reason": "Customer wants a cheaper plan",
        "kb_ids": ["KB-006"],
        "idempotency_key": "plan-key-0001",
    }


def test_plan_change_success_creates_pending_request(registry, migrated_conn, two_customers, ctx) -> None:
    t = two_customers

    result = registry.call("propose_plan_change", _plan_args(t), ticket_id=t.ticket_id, ctx=ctx)

    assert result.ok is True
    assert result.data["status"] == "pending_approval"
    row = migrated_conn.execute(
        "SELECT status, proposed_by FROM plan_change_requests WHERE id = %s",
        (result.data["plan_change_request_id"],),
    ).fetchone()
    assert row["status"] == "pending_approval"
    assert row["proposed_by"] == "agent"


def test_plan_change_proposal_writes_an_audit_row_in_the_same_transaction(
    registry, migrated_conn, two_customers, ctx
) -> None:
    """P4-0.5: a proposal can never exist without its audit trail row."""
    t = two_customers

    result = registry.call("propose_plan_change", _plan_args(t), ticket_id=t.ticket_id, ctx=ctx)

    assert result.ok is True
    audit = migrated_conn.execute(
        "SELECT actor, event, payload FROM audit_log "
        "WHERE entity = 'plan_change_requests' AND entity_id = %s",
        (result.data["plan_change_request_id"],),
    ).fetchall()
    assert len(audit) == 1
    assert audit[0]["actor"] == "agent"
    assert audit[0]["event"] == "plan_change_proposed"
    assert audit[0]["payload"]["to_plan_id"] == "PLAN-STARTER"


def test_plan_change_second_open_request_is_duplicate(registry, two_customers, ctx) -> None:
    t = two_customers

    first = registry.call("propose_plan_change", _plan_args(t), ticket_id=t.ticket_id, ctx=ctx)
    second = registry.call("propose_plan_change", _plan_args(t), ticket_id=t.ticket_id, ctx=ctx)

    assert first.ok is True
    assert second.error.code == ToolErrorCode.DUPLICATE


def test_plan_change_unknown_target_plan_is_not_found(registry, migrated_conn, two_customers, ctx) -> None:
    t = two_customers

    result = registry.call(
        "propose_plan_change", _plan_args(t, to_plan="PLAN-DOES-NOT-EXIST"),
        ticket_id=t.ticket_id, ctx=ctx,
    )

    assert result.error.code == ToolErrorCode.NOT_FOUND
    count = migrated_conn.execute(
        "SELECT count(*) AS n FROM plan_change_requests WHERE ticket_id = %s",
        (t.ticket_id,),
    ).fetchone()["n"]
    assert count == 0


def test_plan_change_to_current_plan_is_business_rule(registry, two_customers, ctx) -> None:
    t = two_customers

    result = registry.call(
        "propose_plan_change", _plan_args(t, to_plan="ITST-PLAN"),
        ticket_id=t.ticket_id, ctx=ctx,
    )

    assert result.error.code == ToolErrorCode.BUSINESS_RULE


def test_plan_change_for_foreign_customer_writes_nothing(registry, migrated_conn, two_customers, ctx) -> None:
    t = two_customers
    before = migrated_conn.execute(
        "SELECT count(*) AS n FROM plan_change_requests"
    ).fetchone()["n"]

    args = _plan_args(t)
    args["customer_id"] = str(t.bob_id)
    result = registry.call("propose_plan_change", args, ticket_id=t.ticket_id, ctx=ctx)

    assert result.error.code == ToolErrorCode.OWNERSHIP_MISMATCH
    after = migrated_conn.execute(
        "SELECT count(*) AS n FROM plan_change_requests"
    ).fetchone()["n"]
    assert after == before


# ---- escalate_to_human -------------------------------------------------------------

def test_escalate_flags_the_ticket(registry, migrated_conn, two_customers, ctx) -> None:
    t = two_customers

    result = registry.call(
        "escalate_to_human",
        {"ticket_id": t.ticket_id, "reason_code": "low_confidence",
         "detail": "Grounding below threshold"},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    assert result.ok is True
    row = migrated_conn.execute(
        "SELECT escalated, agent_outcome FROM tickets WHERE id = %s", (t.ticket_id,)
    ).fetchone()
    assert row["escalated"] is True
    assert row["agent_outcome"] == "escalated"


def test_escalate_unknown_ticket_is_not_found(registry, ctx) -> None:
    result = registry.call(
        "escalate_to_human",
        {"ticket_id": str(uuid.uuid4()), "reason_code": "low_confidence", "detail": "x"},
        ticket_id=str(uuid.uuid4()),
        ctx=ctx,
    )

    assert result.error.code == ToolErrorCode.NOT_FOUND


def test_escalate_writes_one_trace_row(registry, migrated_conn, two_customers, ctx) -> None:
    t = two_customers
    registry.call(
        "escalate_to_human",
        {"ticket_id": t.ticket_id, "reason_code": "injection_suspected", "detail": "x"},
        ticket_id=t.ticket_id,
        ctx=ctx,
    )

    rows = _trace_rows(migrated_conn, ctx.trace_id)
    assert [(r["node"], r["status"]) for r in rows] == [("escalate_to_human", "ok")]
