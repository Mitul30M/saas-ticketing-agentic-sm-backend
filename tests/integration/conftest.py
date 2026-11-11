"""Shared integration fixtures: compose Postgres, migrations, test entities.

Skips (not fails) when the compose Postgres is not reachable.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import db

DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://nimbus:nimbus@localhost:5433/nimbus"
)


def _connect() -> psycopg.Connection:
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


@pytest.fixture(scope="session")
def migrated_conn() -> Any:
    try:
        conn = _connect()
        conn.execute("SELECT 1").fetchone()
    except psycopg.OperationalError:
        pytest.skip("compose Postgres not running (start docker, or set TEST_DATABASE_URL)")
    db.run_migrations(conn)
    return conn


@pytest.fixture(scope="session")
def db_pool() -> Any:
    try:
        pool = ConnectionPool(
            DSN,
            min_size=1,
            max_size=4,
            open=True,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
    except psycopg.OperationalError:
        pytest.skip("compose Postgres not running (start docker, or set TEST_DATABASE_URL)")
    pool.wait(timeout=10.0)
    yield pool
    pool.close()


@pytest.fixture(scope="session", autouse=True)
def _teardown(migrated_conn: Any) -> Any:
    """Remove every ITST-marked fixture row, FK-safe order. Runs once."""
    yield
    conn = migrated_conn
    tickets_uuid = "(SELECT id FROM tickets WHERE subject LIKE 'ITST-%')"
    tickets_text = "(SELECT id::text FROM tickets WHERE subject LIKE 'ITST-%')"
    customers = "(SELECT id FROM customers WHERE external_ref LIKE 'ITST-%')"
    invoices = (
        "(SELECT id FROM invoices WHERE customer_id IN " + customers + ")"
    )
    conn.execute(f"DELETE FROM trace_events WHERE ticket_id::text IN {tickets_text}")
    conn.execute(f"DELETE FROM outbox WHERE ticket_id IN {tickets_uuid}")
    conn.execute(
        "DELETE FROM approvals WHERE ticket_id IN " + tickets_uuid
        + " OR (action_type='refund' AND action_ref IN "
        + "(SELECT id FROM refund_requests WHERE ticket_id IN " + tickets_uuid + "))"
    )
    conn.execute(f"DELETE FROM plan_change_requests WHERE ticket_id IN {tickets_uuid}")
    conn.execute(
        "DELETE FROM refund_requests WHERE ticket_id IN " + tickets_uuid
        + " OR idempotency_key LIKE 'itst-%'"
    )
    conn.execute("DELETE FROM tickets WHERE subject LIKE 'ITST-%'")
    conn.execute(f"DELETE FROM payments WHERE invoice_id IN {invoices}")
    conn.execute(f"DELETE FROM invoices WHERE customer_id IN {customers}")
    conn.execute(f"DELETE FROM subscriptions WHERE customer_id IN {customers}")
    conn.execute("DELETE FROM customers WHERE external_ref LIKE 'ITST-%'")
    conn.execute("DELETE FROM plans WHERE code LIKE 'ITST-%'")


@pytest.fixture()
def core_ids(migrated_conn: Any) -> dict[str, Any]:
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


@pytest.fixture()
def two_customers(migrated_conn: Any) -> SimpleNamespace:
    """Alice (the sender, owns INV_A) and bob (the victim, owns INV_B).

    The ticket's from_email is alice's address: tools must resolve identity
    from the DB, never from arguments (AGENTS invariant 11).
    """
    conn = migrated_conn
    plan = "ITST-PLAN"
    tag = uuid.uuid4().hex[:8]
    alice_ref, bob_ref = f"ITST-A-{tag}", f"ITST-B-{tag}"
    conn.execute(
        "INSERT INTO plans (id, code, name, monthly_price_cents, annual_price_cents) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (plan, plan, "Test Plan", 1000, 10000),
    )
    alice_id = conn.execute(
        "INSERT INTO customers (external_ref, first_name, last_name, email, plan_id) "
        "VALUES (%s,'Alice','Sender',%s,%s) RETURNING id",
        (alice_ref, f"alice.{tag}@example.test", plan),
    ).fetchone()["id"]
    bob_id = conn.execute(
        "INSERT INTO customers (external_ref, first_name, last_name, email, plan_id) "
        "VALUES (%s,'Bob','Victim',%s,%s) RETURNING id",
        (bob_ref, f"bob.{tag}@example.test", plan),
    ).fetchone()["id"]

    def _paid_invoice(letter: str, owner: str, amount: int) -> str:
        num = int(tag, 16) % 100_000_000
        invoice = f"INV-{num:08d}-{letter}"
        sub = conn.execute(
            "INSERT INTO subscriptions (customer_id, plan_id, status, billing_cycle) "
            "VALUES (%s,%s,'active','monthly') RETURNING id",
            (owner, plan),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO invoices (id, customer_id, subscription_id, amount_cents, "
            "status, issued_at) VALUES (%s,%s,%s,%s,'paid',now())",
            (invoice, owner, sub, amount),
        )
        conn.execute(
            "INSERT INTO payments (invoice_id, amount_cents, method, status, paid_at) "
            "VALUES (%s,%s,'visa_4242','succeeded',now())",
            (invoice, amount),
        )
        return invoice

    inv_a = _paid_invoice("A", str(alice_id), 5000)
    bob_amount = 3300
    inv_b = _paid_invoice("B", str(bob_id), bob_amount)
    ticket_id = conn.execute(
        "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, from_name) "
        "VALUES (%s,%s,'body',%s,%s,'Alice Sender') RETURNING id",
        (alice_id, f"ITST-ticket-{tag}", f"itst-ticket-{tag}", f"alice.{tag}@example.test"),
    ).fetchone()["id"]
    return SimpleNamespace(
        ticket_id=str(ticket_id),
        alice_id=alice_id,
        bob_id=bob_id,
        alice_email=f"alice.{tag}@example.test",
        bob_email=f"bob.{tag}@example.test",
        bob_name="Bob Victim",
        inv_a=inv_a,
        inv_b=inv_b,
        bob_amount_cents=bob_amount,
    )
