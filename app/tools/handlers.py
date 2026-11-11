"""Real tool handlers. NEVER raise into the graph: return dicts on success,
raise ToolFailure (typed, caught by the registry) for expected failures.

Identity rule (AGENTS invariant 11): the customer is whoever the ticket was
sent FROM — tickets.from_email in the DB. An email, customer_id or invoice_id
CLAIMED in the arguments is a claim to verify against the sender, never a
lookup key.

Business rules (invariant 12): refund windows are computed against the
injected settings.as_of clock, never now().
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Json

from app.config import settings
from app.tools.registry import ToolFailure
from app.tools.schemas import (
    InvoiceInput,
    LookupCustomerInput,
    RefundProposalInput,
    ToolErrorCode,
)


def _as_of() -> datetime | None:
    """Injected eval clock; None = let Postgres use now()."""
    return settings.as_of


def _sender_id(pool: Any, ticket_id: str) -> str:
    """Resolve the customer from the ticket's TRANSPORT sender (from_email)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT from_email FROM tickets WHERE id = %s", (ticket_id,)
        ).fetchone()
        if row is None or not row["from_email"]:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, "no sender identity on ticket")
        matches = conn.execute(
            "SELECT id FROM customers WHERE lower(email) = lower(%s)",
            (row["from_email"],),
        ).fetchall()
    if len(matches) == 1:
        return str(matches[0]["id"])
    if len(matches) == 0:
        raise ToolFailure(ToolErrorCode.NOT_FOUND, "no customer matches the ticket sender")
    # Deliberate duplicate-email edge case: identity is ambiguous -> fail closed.
    raise ToolFailure(ToolErrorCode.NOT_FOUND, "customer identity is ambiguous; escalate")


def _sender_email(pool: Any, ticket_id: str) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT from_email FROM tickets WHERE id = %s", (ticket_id,)
        ).fetchone()
    if row is None or not row["from_email"]:
        raise ToolFailure(ToolErrorCode.NOT_FOUND, "no sender identity on ticket")
    return str(row["from_email"])


def _require_sender(pool: Any, ticket_id: str, claimed_customer_id: str) -> str:
    """Ownership gate: the claimed customer_id must BE the sender."""
    sender = _sender_id(pool, ticket_id)
    if claimed_customer_id != sender:
        raise ToolFailure(
            ToolErrorCode.OWNERSHIP_MISMATCH,
            "claimed customer does not match the ticket sender",
        )
    return sender


def lookup_customer(pool: Any, args: LookupCustomerInput) -> dict[str, Any]:
    """Resolve the ticket sender to a customer. args.from_email is IGNORED (invariant 11)."""
    sender_email = _sender_email(pool, str(args.ticket_id))
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, email, first_name, last_name, status "
            "FROM customers WHERE lower(email) = lower(%s)",
            (sender_email,),
        ).fetchall()
    if len(rows) != 1:
        if not rows:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, "no customer matches the ticket sender")
        raise ToolFailure(ToolErrorCode.NOT_FOUND, "customer identity is ambiguous; escalate")
    row = rows[0]
    return {
        "customer_id": str(row["id"]),
        "email": row["email"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "status": row["status"],
    }


def get_invoice(pool: Any, args: InvoiceInput) -> dict[str, Any]:
    """Return an invoice ONLY if it belongs to the ticket sender."""
    sender = _require_sender(pool, str(args.ticket_id), str(args.customer_id))
    with pool.connection() as conn:
        # Missing AND foreign invoices return the SAME code: probing which
        # invoice ids exist must be indistinguishable from theft attempts.
        invoice = conn.execute(
            "SELECT id, customer_id, amount_cents, currency, status, issued_at, due_at "
            "FROM invoices WHERE id = %s AND customer_id = %s",
            (args.invoice_id, sender),
        ).fetchone()
        if invoice is None:
            raise ToolFailure(
                ToolErrorCode.OWNERSHIP_MISMATCH, "invoice not found for this customer"
            )
        payments = conn.execute(
            "SELECT amount_cents, amount_refunded_cents, method, status, paid_at "
            "FROM payments WHERE invoice_id = %s ORDER BY paid_at",
            (args.invoice_id,),
        ).fetchall()
    return {
        "invoice_id": invoice["id"],
        "amount_cents": invoice["amount_cents"],
        "currency": invoice["currency"],
        "status": invoice["status"],
        "issued_at": invoice["issued_at"].isoformat(),
        "due_at": invoice["due_at"].isoformat() if invoice["due_at"] else None,
        "payments": [
            {
                "amount_cents": p["amount_cents"],
                "amount_refunded_cents": p["amount_refunded_cents"],
                "method": p["method"],
                "status": p["status"],
                "paid_at": p["paid_at"].isoformat() if p["paid_at"] else None,
            }
            for p in payments
        ],
    }


def _server_idempotency_key(ticket_id: str, invoice_id: str, amount_cents: int) -> str:
    """Agent keys are metadata only; the stored key is derived server-side so a
    replay of the same proposal is a no-op and a different proposal is unique."""
    digest = hashlib.sha256(
        f"refund|{ticket_id}|{invoice_id}|{amount_cents}".encode()
    ).hexdigest()[:32]
    return f"srv-{digest}"


def propose_refund(pool: Any, args: RefundProposalInput) -> dict[str, Any]:
    """Insert a PENDING refund request. Never approves, never executes."""
    sender = _require_sender(pool, str(args.ticket_id), str(args.customer_id))
    with pool.connection() as conn:
        invoice = conn.execute(
            "SELECT id, amount_cents, status, issued_at FROM invoices "
            "WHERE id = %s AND customer_id = %s",
            (args.invoice_id, sender),
        ).fetchone()
        if invoice is None:
            raise ToolFailure(
                ToolErrorCode.OWNERSHIP_MISMATCH, "invoice not found for this customer"
            )
        if invoice["status"] not in ("paid", "partially_refunded"):
            raise ToolFailure(
                ToolErrorCode.BUSINESS_RULE, "refunds require a paid invoice"
            )
        as_of = _as_of()
        window_floor = conn.execute(
            "SELECT COALESCE(%s::timestamptz, now()) - make_interval(days => %s) AS floor",
            (as_of, settings.refund_window_days),
        ).fetchone()["floor"]
        if invoice["issued_at"] < window_floor:
            raise ToolFailure(
                ToolErrorCode.BUSINESS_RULE,
                f"invoice is outside the {settings.refund_window_days}-day refund window",
            )
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS paid, "
            "COALESCE(SUM(amount_refunded_cents), 0) AS refunded "
            "FROM payments WHERE invoice_id = %s AND status = 'succeeded'",
            (args.invoice_id,),
        ).fetchone()
        refundable = paid["paid"] - paid["refunded"]
        if args.amount_cents > refundable:
            raise ToolFailure(
                ToolErrorCode.BUSINESS_RULE,
                f"amount exceeds refundable balance ({refundable} cents)",
            )
        key = _server_idempotency_key(str(args.ticket_id), args.invoice_id, args.amount_cents)
        evidence = {
            "kb_ids": args.kb_ids,
            "refundable_cents": refundable,
            "claimed_idempotency_key": args.idempotency_key,
        }
        try:
            row = conn.execute(
                "INSERT INTO refund_requests "
                "(ticket_id, invoice_id, amount_cents, reason, evidence, idempotency_key) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id, status",
                (
                    str(args.ticket_id),
                    args.invoice_id,
                    args.amount_cents,
                    args.reason,
                    Json(evidence),
                    key,
                ),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            # One open request per invoice (partial index) or a replayed
            # identical proposal (invoice, key) -> typed duplicate, no change.
            raise ToolFailure(
                ToolErrorCode.DUPLICATE,
                "an open refund request already exists for this invoice",
            ) from None
    return {
        "refund_request_id": str(row["id"]),
        "status": row["status"],
        "invoice_id": args.invoice_id,
        "amount_cents": args.amount_cents,
        "idempotency_key": key,
    }
