"""Deterministic seed for the Nimbus business core (PLAN 4.2).

Re-running is idempotent: every primary key is a stable UUID derived from a
fixed namespace plus a natural key, and every INSERT uses
``ON CONFLICT DO NOTHING``. Randomness comes from a fixed Faker seed and a
fixed ``random.Random`` seed, so two runs yield identical rows; relative ages
(invoice windows etc.) stay stable because rows are generated relative to the
seed reference date.

    uv run python -m db.seed.seed
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from faker import Faker

from app.config import settings
from app.db import connect

SEED_FAKER_SEED = 47
SEED_NAMESPACE = uuid.UUID("3e5f8a1c-9b2d-4e67-a1c3-d5f7b9e1c2a4")
REF_DATE = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)

PLANS: list[tuple[str, str, str, int, int, list[str]]] = [
    ("PLAN-FREE", "free", "Free", 0, 0, ["1 seat", "community support"]),
    ("PLAN-STARTER", "starter", "Starter", 1999, 19190, ["3 seats", "email support", "10 GB docs"]),
    ("PLAN-PRO", "pro", "Pro", 4999, 47990, ["10 seats", "priority support", "SSO"]),
    ("PLAN-BUSINESS", "business", "Business", 12999, 124990, ["50 seats", "SLA", "audit log", "SCIM"]),
]
PLAN_MONTHLY = {p[0]: p[3] for p in PLANS}

N_CUSTOMERS = 100
PAY_METHODS = ["visa_4242", "visa_1111", "mc_5555", "amex_0005", "ach_demo"]


def det_uuid(kind: str, *parts: str) -> UUID:
    """Stable UUID for a seeded entity (what makes reseeding idempotent)."""
    return uuid5(SEED_NAMESPACE, "::".join((kind, *parts)))


def plan_for_index(i: int) -> str:
    """Deterministic weighted mix: 5 free, 7 starter, 5 pro, 3 business per 20."""
    bucket = i % 20
    if bucket < 5:
        return "PLAN-FREE"
    if bucket < 12:
        return "PLAN-STARTER"
    if bucket < 17:
        return "PLAN-PRO"
    return "PLAN-BUSINESS"


def seed_plans(conn: Any) -> None:
    for pid, code, name, monthly, annual, features in PLANS:
        conn.execute(
            """
            INSERT INTO plans (id, code, name, monthly_price_cents, annual_price_cents, features)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (pid, code, name, monthly, annual, json.dumps(features)),
        )


def seed_customers(conn: Any, fake: Faker) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(1, N_CUSTOMERS + 1):
        first, last = fake.first_name(), fake.last_name()
        email = f"{first.lower()}.{last.lower()}.{i}@example.nimbus.test"
        # The last two are deliberately suspended/closed demo accounts;
        # every other exception (past_due, dup email, ...) lives in edge_cases.
        status = "suspended" if i == 98 else "closed" if i == 99 else "active"
        created_at = REF_DATE - timedelta(days=90 + (i * 3) % 400, hours=i % 5)
        row = {
            "id": det_uuid("customer", f"C-{i:04d}"),
            "ref": f"C-{i:04d}",
            "first": first,
            "last": last,
            "email": email,
            "status": status,
            "plan": plan_for_index(i),
            "created_at": created_at,
        }
        rows.append(row)
        conn.execute(
            """
            INSERT INTO customers
                (id, external_ref, first_name, last_name, email, status, language, plan_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'en', %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                row["id"],
                row["ref"],
                row["first"],
                row["last"],
                row["email"],
                row["status"],
                row["plan"],
                row["created_at"],
            ),
        )
    return rows


def seed_subscriptions(conn: Any, customers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One main sub per customer + canceled history rows (~150 total)."""
    subs: list[dict[str, Any]] = []
    period_start = REF_DATE.replace(day=1)

    def insert(sub_id: UUID, cust: dict[str, Any], plan: str, status: str, cycle: str,
               start: datetime | None, end: datetime | None, canceled: datetime | None,
               key: str) -> None:
        subs.append({"id": sub_id, "customer": cust, "plan": plan, "key": key,
                     "cycle": cycle, "status": status, "start": start})
        conn.execute(
            """
            INSERT INTO subscriptions
                (id, customer_id, plan_id, status, billing_cycle,
                 current_period_start, current_period_end, canceled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (sub_id, cust["id"], plan, status, cycle, start, end, canceled),
        )

    for i, c in enumerate(customers, start=1):
        cycle = "annual" if (i % 7 == 0 and c["plan"] in ("PLAN-PRO", "PLAN-BUSINESS")) else "monthly"
        if c["status"] == "closed":
            canceled_at = REF_DATE - timedelta(days=20)
            start = REF_DATE - timedelta(days=400)
            insert(det_uuid("sub", c["ref"], "main"), c, c["plan"], "canceled",
                   cycle, start, None, canceled_at, "main")
        else:
            status = "trialing" if i in (95, 96, 97) else "active"
            end = start = period_start
            if status == "active":
                end = start + (timedelta(days=365) if cycle == "annual" else timedelta(days=30))
            insert(det_uuid("sub", c["ref"], "main"), c, c["plan"], status, cycle,
                   start if status != "trialing" else None, end, None, "main")

    # Canceled history subs on a different plan: tools must scope by status.
    hist_plan = {"PLAN-FREE": "PLAN-STARTER", "PLAN-STARTER": "PLAN-PRO",
                 "PLAN-PRO": "PLAN-STARTER", "PLAN-BUSINESS": "PLAN-PRO"}
    for c in customers[:44]:
        hstart = REF_DATE - timedelta(days=500)
        insert(det_uuid("sub", c["ref"], "hist"), c, hist_plan[c["plan"]], "canceled",
               "monthly", hstart, hstart + timedelta(days=30),
               hstart + timedelta(days=37), "hist")
    return subs


def main_sub_map(subs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """customer external_ref -> their non-historical subscription."""
    return {s["customer"]["ref"]: s for s in subs if s["key"] == "main"}


def seed_invoices_and_payments(
    conn: Any,
    customers: list[dict[str, Any]],
    subs: dict[str, dict[str, Any]],
    rng: random.Random,
) -> None:
    n_inv = n_pay = 0
    for c in customers:
        sub = subs.get(c["ref"])
        if sub is None or sub["status"] != "active" or c["status"] != "active":
            continue
        plan = sub["plan"]
        # Free plan only shows a seats add-on invoice (monthly).
        amounts: list[tuple[str, int]] = []
        if sub["cycle"] == "annual":
            amounts.append(("A1", PLAN_MONTHLY[plan] * 12 - 1000))
        else:
            n_periods = 4
            base = PLAN_MONTHLY[plan] if plan != "PLAN-FREE" else 1999
            amounts = [(f"M{m}", base + m) for m in range(1, n_periods + 1)]
        for suffix, amount in amounts:
            if sub["cycle"] == "annual":
                issued = sub["start"]
            else:
                m = int(suffix[1:])
                issued = REF_DATE - timedelta(days=30 * m)
            invoice_id = f"INV-{c['ref'][2:]}-{suffix}"
            # Newest invoice varies (paid/open/void); older ones are paid.
            is_newest = suffix in ("M4", "A1")
            status = rng.choices(["paid", "open", "void"], weights=[80, 15, 5])[0] if is_newest else "paid"
            due = issued + timedelta(days=14)
            conn.execute(
                """
                INSERT INTO invoices
                    (id, customer_id, subscription_id, amount_cents, currency, status, issued_at, due_at)
                VALUES (%s, %s, %s, %s, 'USD', %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (invoice_id, c["id"], sub["id"], amount, status, issued, due),
            )
            n_inv += 1
            if status == "paid":
                method = rng.choice(PAY_METHODS)
                paid_at = issued + timedelta(hours=6 + (m if suffix.startswith("M") else 1))
                conn.execute(
                    """
                    INSERT INTO payments
                        (id, invoice_id, amount_cents, method, status, paid_at, amount_refunded_cents)
                    VALUES (%s, %s, %s, %s, 'succeeded', %s, 0)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (det_uuid("pay", invoice_id), invoice_id, amount, method, paid_at),
                )
                n_pay += 1
    print(f"  invoices: {n_inv}, payments: {n_pay}")


def count_rows(conn: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    # Table names come from this fixed list, never from user input.
    for table in ("plans", "customers", "subscriptions", "invoices", "payments", "tickets"):
        row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        out[table] = int(row["n"]) if row else 0
    return out


def main() -> None:
    fake = Faker("en_US")
    fake.seed_instance(SEED_FAKER_SEED)
    rng = random.Random(SEED_FAKER_SEED)
    with connect(settings.database_url) as conn:
        print(f"Seeding (reference date: {REF_DATE:%Y-%m-%d}) ...")
        seed_plans(conn)
        customers = seed_customers(conn, fake)
        print(f"  customers: {len(customers)}")
        subs = seed_subscriptions(conn, customers)
        print(f"  subscriptions: {len(subs)}")
        seed_invoices_and_payments(conn, customers, main_sub_map(subs), rng)
        from db.seed.edge_cases import seed_edge_cases

        seed_edge_cases(conn, customers, main_sub_map(subs))
        counts = count_rows(conn)
        print("Row counts:", counts)
        assert counts["plans"] == 4, "expected exactly 4 plans"
        assert counts["customers"] == N_CUSTOMERS + 1, "101 = 100 base + 1 duplicate-email edge"


if __name__ == "__main__":
    main()
