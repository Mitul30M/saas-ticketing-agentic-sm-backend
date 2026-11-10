"""DB integration tests: migrations + the safety triggers of 003_support.sql.

Skips (not fails) when compose Postgres is not reachable.
These run the real DDL and exercise two DB-level safety mechanisms:
  - refund state machine guard (pending_approval -> approved -> executed only)
  - audit_log append-only trigger
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app import db

DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://nimbus:nimbus@localhost:5433/nimbus"
)


def _connect() -> psycopg.Connection:
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


@pytest.fixture(scope="session")
def migrated_conn():
    try:
        conn = _connect()
        conn.execute("SELECT 1").fetchone()
    except psycopg.OperationalError:
        pytest.skip("compose Postgres not running (start docker, or set TEST_DATABASE_URL)")
    db.run_migrations(conn)
    return conn


@pytest.fixture()
def core_ids(migrated_conn):
    """Minimal plan -> customer -> subscription -> invoice -> ticket chain."""
    conn = migrated_conn
    plan = "ITST-PLAN"
    cust_ref = f"ITST-{uuid.uuid4().hex[:8]}"
    invoice = f"INV-ITST{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO plans (id, code, name, monthly_price_cents, annual_price_cents) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (plan, plan, "Test Plan", 1000, 10000),
    )
    cust = conn.execute(
        "INSERT INTO customers (external_ref, first_name, last_name, email, plan_id) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (cust_ref, "Test", "Customer", f"{cust_ref}@example.test", plan),
    ).fetchone()["id"]
    sub = conn.execute(
        "INSERT INTO subscriptions (customer_id, plan_id, status, billing_cycle) "
        "VALUES (%s,%s,'active','monthly') RETURNING id",
        (cust, plan),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO invoices (id, customer_id, subscription_id, amount_cents, status, issued_at) "
        "VALUES (%s,%s,%s,%s,'paid',now())",
        (invoice, cust, sub, 5000),
    )
    ticket = conn.execute(
        "INSERT INTO tickets (subject, body, content_hash) "
        "VALUES (%s,%s,%s) RETURNING id",
        (f"ITST-integration {invoice}", f"test body for {invoice}", f"itst-{invoice}"),
    ).fetchone()["id"]
    return {"cust_id": cust, "invoice": invoice, "ticket_id": ticket}


# ---- schema shape -----------------------------------------------------------

def test_all_tables_exist(migrated_conn) -> None:
    expected = {
        "plans", "customers", "subscriptions", "invoices", "payments", "tickets",
        "refund_requests", "plan_change_requests", "approvals", "audit_log", "outbox",
        "trace_events", "idempotency_keys", "llm_cache", "jobs",
        "kb_articles", "kb_chunks", "schema_migrations",
    }
    rows = migrated_conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    ).fetchall()
    got = {r["tablename"] for r in rows}
    assert expected <= got


def test_vector_extension_available(migrated_conn) -> None:
    row = migrated_conn.execute(
        "SELECT extname FROM pg_extension WHERE extname='vector'"
    ).fetchone()
    assert row is not None


# ---- safety: refund state machine ------------------------------------------

def _insert_refund(conn, core_ids, key: str, amount: int = 1000) -> None:
    conn.execute(
        "INSERT INTO refund_requests (ticket_id, invoice_id, amount_cents, reason, "
        "evidence, idempotency_key) VALUES (%s,%s,%s,%s,%s,%s)",
        (core_ids["ticket_id"], core_ids["invoice"], amount,
         "test reason here", '["x"]', key),
    )


def test_refund_insert_must_start_pending(migrated_conn, core_ids) -> None:
    from psycopg.errors import RaiseException

    with pytest.raises(RaiseException, match="pending_approval"):
        migrated_conn.execute(
            "INSERT INTO refund_requests (ticket_id, invoice_id, amount_cents, reason, "
            "evidence, status, idempotency_key) VALUES (%s,%s,%s,%s,%s,'approved','itst-a1')",
            (core_ids["ticket_id"], core_ids["invoice"], 1000,
             "test reason here", '["x"]'),
        )


def test_refund_valid_transitions(migrated_conn, core_ids) -> None:
    _insert_refund(migrated_conn, core_ids, "itst-t1")
    migrated_conn.execute(
        "INSERT INTO approvals "
        "(ticket_id, action_type, action_ref, decision, decided_by) "
        "SELECT ticket_id, 'refund', id, 'approved', 'ops-test' "
        "FROM refund_requests WHERE idempotency_key='itst-t1' RETURNING id"
    ).fetchone()["id"]
    migrated_conn.execute(
        "UPDATE refund_requests SET status='approved', approved_by='ops-test', "
        "decided_at=now() WHERE idempotency_key='itst-t1'"
    )
    migrated_conn.execute(
        "UPDATE refund_requests SET status='executed', executed_at=now() "
        "WHERE idempotency_key='itst-t1'"
    )
    from psycopg.errors import RaiseException

    with pytest.raises(RaiseException, match="illegal"):
        migrated_conn.execute(
            "UPDATE refund_requests SET status='rejected' WHERE idempotency_key='itst-t1'"
        )


def test_refund_approval_requires_human_decision(migrated_conn, core_ids) -> None:
    from psycopg.errors import CheckViolation, RaiseException

    _insert_refund(migrated_conn, core_ids, "itst-no-approval")
    with pytest.raises((CheckViolation, RaiseException), match="approval|decision"):
        migrated_conn.execute(
            "UPDATE refund_requests SET status='approved', approved_by='ops-test' "
            "WHERE idempotency_key='itst-no-approval'"
        )


def test_refund_execution_requires_timestamp(migrated_conn, core_ids) -> None:
    from psycopg.errors import CheckViolation

    _insert_refund(migrated_conn, core_ids, "itst-no-executed-at")
    migrated_conn.execute(
        "INSERT INTO approvals "
        "(ticket_id, action_type, action_ref, decision, decided_by) "
        "SELECT ticket_id, 'refund', id, 'approved', 'ops-test' "
        "FROM refund_requests WHERE idempotency_key='itst-no-executed-at'"
    )
    migrated_conn.execute(
        "UPDATE refund_requests SET status='approved', approved_by='ops-test' "
        "WHERE idempotency_key='itst-no-executed-at'"
    )
    with pytest.raises(CheckViolation, match="executed_needs_timestamp"):
        migrated_conn.execute(
            "UPDATE refund_requests SET status='executed' "
            "WHERE idempotency_key='itst-no-executed-at'"
        )


def test_plan_change_cannot_jump_to_executed(migrated_conn, core_ids) -> None:
    from psycopg.errors import RaiseException

    migrated_conn.execute(
        "INSERT INTO plan_change_requests "
        "(ticket_id, customer_id, from_plan_id, to_plan_id, idempotency_key) "
        "VALUES (%s, %s, 'ITST-PLAN', 'PLAN-STARTER', 'itst-plan-jump')",
        (core_ids["ticket_id"], core_ids["cust_id"]),
    )
    with pytest.raises(RaiseException, match="illegal"):
        migrated_conn.execute(
            "UPDATE plan_change_requests SET status='executed' "
            "WHERE idempotency_key='itst-plan-jump'"
        )


def test_final_approval_and_outbox_are_idempotent(migrated_conn, core_ids) -> None:
    from psycopg.errors import IntegrityError

    _insert_refund(migrated_conn, core_ids, "itst-final-once")
    approval = migrated_conn.execute(
        "INSERT INTO approvals "
        "(ticket_id, action_type, action_ref, decision, decided_by) "
        "SELECT ticket_id, 'refund', id, 'approved', 'ops-test' "
        "FROM refund_requests WHERE idempotency_key='itst-final-once' RETURNING id"
    ).fetchone()["id"]
    with pytest.raises(IntegrityError):
        migrated_conn.execute(
            "INSERT INTO approvals "
            "(ticket_id, action_type, action_ref, decision, decided_by) "
            "SELECT ticket_id, 'refund', action_ref, 'approved', 'ops-test' "
            "FROM approvals WHERE id=%s",
            (approval,),
        )
    migrated_conn.execute(
        "INSERT INTO outbox (ticket_id, approval_id, approved_by, body) "
        "VALUES (%s, %s, 'ops-test', 'approved reply')",
        (core_ids["ticket_id"], approval),
    )
    with pytest.raises(IntegrityError):
        migrated_conn.execute(
            "INSERT INTO outbox (ticket_id, approval_id, approved_by, body) "
            "VALUES (%s, %s, 'ops-test', 'duplicate reply')",
            (core_ids["ticket_id"], approval),
        )


def test_refund_illegal_jump_rejected(migrated_conn, core_ids) -> None:
    from psycopg.errors import RaiseException

    _insert_refund(migrated_conn, core_ids, "itst-j1")
    with pytest.raises(RaiseException, match="illegal"):
        migrated_conn.execute(
            "UPDATE refund_requests SET status='executed' WHERE idempotency_key='itst-j1'"
        )


def test_only_one_open_refund_per_invoice(migrated_conn, core_ids) -> None:
    from psycopg.errors import IntegrityError

    _insert_refund(migrated_conn, core_ids, "itst-d1")
    with pytest.raises(IntegrityError):
        _insert_refund(migrated_conn, core_ids, "itst-d2")


def test_rejected_refund_allows_new_proposal(migrated_conn, core_ids) -> None:
    _insert_refund(migrated_conn, core_ids, "itst-r1")
    migrated_conn.execute(
        "UPDATE refund_requests SET status='rejected', decided_at=now() "
        "WHERE idempotency_key='itst-r1'"
    )
    _insert_refund(migrated_conn, core_ids, "itst-r2")


# ---- safety: audit_log append-only -----------------------------------------

def test_audit_log_is_append_only(migrated_conn) -> None:
    from psycopg.errors import RaiseException

    migrated_conn.execute(
        "INSERT INTO audit_log (actor, event, entity, entity_id, payload) "
        "VALUES ('itst','test_event','t','1','{}')"
    )
    row = migrated_conn.execute(
        "SELECT id FROM audit_log WHERE actor='itst' AND event='test_event' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    with pytest.raises(RaiseException, match="append-only"):
        migrated_conn.execute(
            "UPDATE audit_log SET event='changed' WHERE id=%s", (row["id"],)
        )
    with pytest.raises(RaiseException, match="append-only"):
        migrated_conn.execute("DELETE FROM audit_log WHERE id=%s", (row["id"],))


# ---- cleanup ---------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _teardown(migrated_conn):
    yield
    migrated_conn.execute("DELETE FROM outbox WHERE ticket_id IN "
                          "(SELECT id FROM tickets WHERE subject LIKE 'ITST-%')")
    migrated_conn.execute("DELETE FROM approvals WHERE ticket_id IN "
                          "(SELECT id FROM tickets WHERE subject LIKE 'ITST-%')")
    migrated_conn.execute("DELETE FROM plan_change_requests WHERE ticket_id IN "
                          "(SELECT id FROM tickets WHERE subject LIKE 'ITST-%')")
    migrated_conn.execute("DELETE FROM refund_requests WHERE idempotency_key LIKE 'itst-%'")
    migrated_conn.execute("DELETE FROM tickets WHERE subject LIKE 'ITST-%'")
    migrated_conn.execute("DELETE FROM outbox WHERE ticket_id IN "
                          "(SELECT id FROM tickets WHERE subject LIKE 'ITST-%')")
    migrated_conn.execute("DELETE FROM invoices WHERE id LIKE 'INV-ITST%'")
    migrated_conn.execute("DELETE FROM subscriptions WHERE customer_id IN "
                          "(SELECT id FROM customers WHERE external_ref LIKE 'ITST-%')")
    migrated_conn.execute("DELETE FROM customers WHERE external_ref LIKE 'ITST-%'")
    migrated_conn.execute("DELETE FROM plans WHERE code LIKE 'ITST-%'")
    migrated_conn.close()
