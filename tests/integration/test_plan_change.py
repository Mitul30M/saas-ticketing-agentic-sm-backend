"""Plan-change decisions, immediate execution, and deferred application."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.routes import decide
from app.api.schemas import DecisionBody
from app.services.approvals import decide_plan_change
from app.services.executor import apply_due_plan_changes, execute_plan_change
from app.tools.registry import ToolFailure, build_registry
from app.tools.schemas import ToolContext, ToolErrorCode

pytestmark = pytest.mark.integration

FROM_PLAN = "ITST-PLAN-FROM"
TO_PLAN = "ITST-PLAN-TO"


@pytest.fixture()
def plan_customer(migrated_conn: Any) -> SimpleNamespace:
    tag = uuid.uuid4().hex[:8]
    for plan_id in (FROM_PLAN, TO_PLAN):
        migrated_conn.execute(
            "INSERT INTO plans (id, code, name, monthly_price_cents, annual_price_cents) "
            "VALUES (%s,%s,%s,1000,10000) ON CONFLICT (id) DO NOTHING",
            (plan_id, plan_id, plan_id),
        )
    customer_id = migrated_conn.execute(
        "INSERT INTO customers (external_ref, first_name, last_name, email, plan_id) "
        "VALUES (%s,'Plan','Customer',%s,%s) RETURNING id",
        (f"ITST-PLANC-{tag}", f"plan.{tag}@example.test", FROM_PLAN),
    ).fetchone()["id"]
    migrated_conn.execute(
        "INSERT INTO subscriptions (customer_id, plan_id, status, billing_cycle, current_period_end) "
        "VALUES (%s,%s,'active','monthly', now() + interval '10 days')",
        (customer_id, FROM_PLAN),
    )
    ticket_id = migrated_conn.execute(
        "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, status) "
        "VALUES (%s,%s,%s,%s,%s,'awaiting_approval') RETURNING id",
        (
            customer_id,
            f"ITST plan change {tag}",
            "Please move me to the next plan.",
            f"itst-plan-{tag}",
            f"plan.{tag}@example.test",
        ),
    ).fetchone()["id"]
    return SimpleNamespace(
        customer_id=str(customer_id), ticket_id=str(ticket_id), email=f"plan.{tag}@example.test"
    )


def _propose(pool: Any, fixture: SimpleNamespace, effective: str = "now") -> str:
    result = build_registry(pool).call(
        "propose_plan_change",
        {
            "ticket_id": fixture.ticket_id,
            "customer_id": fixture.customer_id,
            "to_plan_id": TO_PLAN,
            "effective": effective,
            "reason": "Customer asked for the next plan.",
            "kb_ids": ["KB-010"],
            "idempotency_key": f"itst-plan-{fixture.ticket_id}",
        },
        ticket_id=fixture.ticket_id,
        ctx=ToolContext(trace_id=f"plan-{fixture.ticket_id}", timeout_s=5),
    )
    assert result.ok is True, result.error
    return str(result.data["plan_change_request_id"])


def _audit(conn: Any, event: str, ref: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE event=%s AND entity_id=%s", (event, ref)
    ).fetchone()["n"]


def _subscription(conn: Any, customer_id: str) -> dict[str, Any]:
    return conn.execute(
        "SELECT plan_id, scheduled_plan_id, scheduled_effective_at, scheduled_plan_change_id "
        "FROM subscriptions WHERE customer_id=%s AND status='active'",
        (customer_id,),
    ).fetchone()


def test_approved_plan_change_executes_once_and_audits(
    db_pool: Any, migrated_conn: Any, plan_customer: SimpleNamespace
) -> None:
    ref = _propose(db_pool, plan_customer, effective="now")
    key = f"itst-plan-decide-{ref}"
    first = decide(
        "plan_change", ref, DecisionBody(decision="approve"), None, db_pool, "mia", key
    )
    replay = decide(
        "plan_change", ref, DecisionBody(decision="approve"), None, db_pool, "mia", key
    )
    assert replay == first

    request = migrated_conn.execute(
        "SELECT status, approved_by, executed_at FROM plan_change_requests WHERE id=%s", (ref,)
    ).fetchone()
    assert request["status"] == "executed"
    assert request["approved_by"] == "mia"
    assert request["executed_at"] is not None
    assert _subscription(migrated_conn, plan_customer.customer_id)["plan_id"] == TO_PLAN
    assert migrated_conn.execute(
        "SELECT plan_id FROM customers WHERE id=%s", (plan_customer.customer_id,)
    ).fetchone()["plan_id"] == TO_PLAN
    assert _audit(migrated_conn, "plan_change_decision_recorded", ref) == 1
    assert _audit(migrated_conn, "plan_change_executed", ref) == 1


def test_rejected_plan_change_moves_nothing(
    db_pool: Any, migrated_conn: Any, plan_customer: SimpleNamespace
) -> None:
    ref = _propose(db_pool, plan_customer, effective="now")
    decide(
        "plan_change", ref, DecisionBody(decision="reject"), None, db_pool, "mia",
        f"itst-plan-reject-{ref}",
    )
    assert migrated_conn.execute(
        "SELECT status FROM plan_change_requests WHERE id=%s", (ref,)
    ).fetchone()["status"] == "rejected"
    subscription = _subscription(migrated_conn, plan_customer.customer_id)
    assert subscription["plan_id"] == FROM_PLAN
    assert subscription["scheduled_plan_id"] is None
    assert _audit(migrated_conn, "plan_change_executed", ref) == 0


def test_next_cycle_plan_change_is_scheduled_then_applied(
    db_pool: Any, migrated_conn: Any, plan_customer: SimpleNamespace
) -> None:
    ref = _propose(db_pool, plan_customer, effective="next_cycle")
    decide(
        "plan_change", ref, DecisionBody(decision="approve"), None, db_pool, "mia",
        f"itst-plan-schedule-{ref}",
    )
    scheduled = _subscription(migrated_conn, plan_customer.customer_id)
    assert scheduled["plan_id"] == FROM_PLAN
    assert scheduled["scheduled_plan_id"] == TO_PLAN
    assert scheduled["scheduled_effective_at"] is not None
    assert str(scheduled["scheduled_plan_change_id"]) == ref

    migrated_conn.execute(
        "UPDATE subscriptions SET scheduled_effective_at = now() - interval '1 minute' "
        "WHERE customer_id=%s",
        (plan_customer.customer_id,),
    )
    assert apply_due_plan_changes(db_pool) == 1
    applied = _subscription(migrated_conn, plan_customer.customer_id)
    assert applied["plan_id"] == TO_PLAN
    assert applied["scheduled_plan_id"] is None
    assert applied["scheduled_plan_change_id"] is None
    assert _audit(migrated_conn, "plan_change_scheduled_applied", ref) == 1
    # a replayed tick is a no-op
    assert apply_due_plan_changes(db_pool) == 0
    assert _audit(migrated_conn, "plan_change_scheduled_applied", ref) == 1


def test_executor_refuses_when_the_subscription_changed_since_proposal(
    db_pool: Any, migrated_conn: Any, plan_customer: SimpleNamespace
) -> None:
    ref = _propose(db_pool, plan_customer, effective="now")
    migrated_conn.execute(
        "UPDATE subscriptions SET plan_id=%s WHERE customer_id=%s",
        (TO_PLAN, plan_customer.customer_id),
    )
    approval_id = decide_plan_change(db_pool, ref, "mia", "approved", None)
    assert approval_id
    with pytest.raises(ToolFailure) as exc:
        execute_plan_change(db_pool, request_id=ref, approved_by="mia")
    assert exc.value.code == ToolErrorCode.BUSINESS_RULE
    assert _audit(migrated_conn, "plan_change_executed", ref) == 0
