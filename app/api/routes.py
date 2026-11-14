"""HTTP routes; services own validation, writes, and approval transitions."""

from __future__ import annotations

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query

from app.api.dependencies import Pool, Protected
from app.api.schemas import DecisionAccepted, DecisionBody, TicketCreate, TicketCreated
from app.services.approvals import (
    decide_plan_change,
    decide_refund,
    record_reply_decision,
)
from app.services.executor import execute_plan_change, execute_refund
from app.services.idempotency import execute_once
from app.services.intake import create_ticket
from app.services.jobs import enqueue, enqueue_rerun
from app.tools.registry import ToolFailure
from app.tools.schemas import ToolErrorCode

router = APIRouter(prefix="/api")


def _failure(exc: ToolFailure) -> HTTPException:
    code = exc.code.value
    status = {
        "not_found": 404,
        "duplicate": 409,
        "business_rule": 409,
        "internal": 500,
    }.get(code, 422)
    headers = {"Retry-After": "1"} if exc.retryable else None
    return HTTPException(status_code=status, detail=exc.message, headers=headers)


@router.post("/tickets", response_model=TicketCreated, status_code=201)
def post_ticket(
    payload: TicketCreate,
    pool: Pool,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> TicketCreated:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    try:
        status, body, _ = execute_once(
            pool,
            key=idempotency_key,
            endpoint="POST:/api/tickets",
            body=payload.model_dump(mode="json"),
            handler=lambda: (201, create_ticket(pool, payload.model_dump(mode="json"))),
        )
    except ToolFailure as exc:
        raise _failure(exc) from exc
    if status != 201:
        raise HTTPException(status_code=status, detail=body)
    return TicketCreated.model_validate(body)


@router.get("/tickets")
def list_tickets(
    _: Protected,
    pool: Pool,
    status: str | None = None,
    queue: str | None = None,
    escalated: bool | None = None,
    handled_by: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    clauses: list[str] = []
    args: list[Any] = []
    if status:
        clauses.append("status = %s")
        args.append(status)
    if queue:
        clauses.append("queue = %s")
        args.append(queue)
    if escalated is not None:
        clauses.append("escalated = %s")
        args.append(escalated)
    if handled_by:
        clauses.append("handled_by = %s")
        args.append(handled_by)
    if q:
        clauses.append("(subject ILIKE %s OR body ILIKE %s)")
        args.extend([f"%{q}%", f"%{q}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.extend([limit, (page - 1) * limit])
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT id, subject, status, queue, category, priority, sentiment, escalated, "
            f"handled_by, received_at FROM tickets {where} ORDER BY received_at DESC "
            f"LIMIT %s OFFSET %s", args
        ).fetchall()
    return {"items": rows, "page": page, "limit": limit}


@router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: UUID, _: Protected, pool: Pool) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, subject, body, status, queue, category, priority, sentiment, "
            "triage_reasons, kb_supported, draft_reply, draft_citations, escalated, "
            "escalation_reason, injection_suspected, flags, handled_by, agent_outcome, "
            "received_at, from_email, from_name, customer_id "
            "FROM tickets WHERE id = %s", (str(ticket_id),)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        actions = conn.execute(
            "SELECT 'refund' AS kind, id AS ref, status, amount_cents, invoice_id, "
            "evidence, NULL::text AS from_plan_id, NULL::text AS to_plan_id, NULL::text AS effective "
            "FROM refund_requests WHERE ticket_id=%s "
            "UNION ALL SELECT 'plan_change', id, status, NULL, NULL, evidence, from_plan_id, "
            "to_plan_id, effective FROM plan_change_requests WHERE ticket_id=%s",
            (str(ticket_id), str(ticket_id)),
        ).fetchall()
        # What was actually sent (possibly an edited draft). Simulated delivery, so
        # the dashboard can show the message the customer received, not the draft.
        sent = conn.execute(
            "SELECT body, approved_by, delivered_at FROM outbox WHERE ticket_id = %s "
            "ORDER BY delivered_at DESC NULLS LAST, id DESC LIMIT 1",
            (str(ticket_id),),
        ).fetchone()
    result = dict(row)
    result["proposed_actions"] = [dict(action) for action in actions]
    result["sent_reply"] = dict(sent) if sent is not None else None
    return result


@router.get("/tickets/{ticket_id}/trace")
def ticket_trace(ticket_id: UUID, _: Protected, pool: Pool) -> dict[str, Any]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT trace_id, node, kind, status, error, duration_ms, tokens_in, "
            "tokens_out, cost_usd, cache_hit, started_at, output FROM trace_events "
            "WHERE ticket_id = %s ORDER BY id", (str(ticket_id),)
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        # A failure used to carry only the code ("business_rule"), which tells an
        # operator nothing. The tool's message is stored in output.error; expose
        # just that sentence instead of the whole payload (input/output can be
        # large or contain ticket text).
        detail = None
        if item["status"] == "error" and isinstance(item.get("output"), dict):
            detail = (item["output"].get("error") or {}).get("message")
        item["error_detail"] = detail
        item.pop("output", None)
        items.append(item)
    return {"items": items}


@router.post("/tickets/{ticket_id}/run", status_code=202)
def rerun_ticket(
    ticket_id: UUID,
    _: Protected,
    pool: Pool,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    try:
        _status, body, _replay = execute_once(
            pool, key=idempotency_key, endpoint=f"POST:/api/tickets/{ticket_id}/run",
            body={"ticket_id": str(ticket_id)},
            handler=lambda: (202, {"ticket_id": str(ticket_id), "job_id": enqueue_rerun(pool, str(ticket_id))[0]}),
        )
    except ToolFailure as exc:
        raise _failure(exc) from exc
    return cast(dict[str, Any], body)


@router.get("/approvals/pending")
def pending_approvals(_: Protected, pool: Pool) -> dict[str, Any]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT 'refund' AS kind, r.id AS ref, r.ticket_id, r.status, r.amount_cents, "
            "r.invoice_id, r.evidence, t.subject, t.draft_reply, t.draft_citations, "
            "t.flags, t.injection_suspected, NULL::text AS from_plan_id, NULL::text AS to_plan_id, "
            "NULL::text AS effective FROM refund_requests r JOIN tickets t ON t.id=r.ticket_id "
            "WHERE r.status='pending_approval' "
            "UNION ALL SELECT 'plan_change', p.id, p.ticket_id, p.status, NULL, NULL, "
            "p.evidence, t.subject, t.draft_reply, t.draft_citations, t.flags, t.injection_suspected, "
            "p.from_plan_id, p.to_plan_id, p.effective "
            "FROM plan_change_requests p JOIN tickets t ON t.id=p.ticket_id "
            "WHERE p.status='pending_approval' "
            "UNION ALL SELECT 'send_reply', t.id, t.id, 'pending_approval', NULL, NULL, "
            "jsonb_build_object('citations', t.draft_citations, 'flags', t.flags), t.subject, "
            "t.draft_reply, t.draft_citations, t.flags, t.injection_suspected, NULL, NULL, NULL "
            "FROM tickets t WHERE t.status='awaiting_approval' AND t.draft_reply IS NOT NULL "
            "ORDER BY ticket_id"
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/customers/{customer_id}")
def customer_detail(customer_id: UUID, _: Protected, pool: Pool) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, external_ref, first_name, last_name, email, status, language, plan_id "
            "FROM customers WHERE id=%s", (str(customer_id),)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="customer not found")
    return dict(row)


@router.get("/kb/articles")
def kb_articles(_: Protected, pool: Pool, limit: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, title, updated FROM kb_articles ORDER BY id LIMIT %s", (limit,)
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/kb/articles/{kb_id}")
def kb_article(kb_id: str, _: Protected, pool: Pool) -> dict[str, Any]:
    """Full article, so a `[KB-xxx]` citation in a draft can be opened."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, title, category, updated, body FROM kb_articles WHERE id = %s",
            (kb_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="kb article not found")
    return dict(row)


@router.get("/kb/search")
def kb_search(
    _: Protected, pool: Pool, q: str = Query(min_length=3, max_length=2000),
) -> dict[str, Any]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT kb_id, chunk_idx, title, text, updated_at FROM kb_chunks "
            "WHERE tsv @@ websearch_to_tsquery('english', %s) "
            "ORDER BY ts_rank(tsv, websearch_to_tsquery('english', %s)) DESC LIMIT 10",
            (q, q),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.post("/approvals/{kind}/{ref}/decide", response_model=DecisionAccepted, status_code=202)
def decide(
    kind: str,
    ref: UUID,
    payload: DecisionBody,
    _: Protected,
    pool: Pool,
    operator: Annotated[str | None, Header(alias="X-Operator")] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> DecisionAccepted:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    if not operator or not operator.strip():
        raise HTTPException(status_code=400, detail="X-Operator is required")
    if kind not in {"refund", "plan_change", "send_reply"}:
        raise HTTPException(status_code=404, detail="unknown approval kind")
    if payload.decision == "edit" and kind != "send_reply":
        raise HTTPException(status_code=422, detail="edit is only valid for send_reply")
    if payload.decision == "edit" and not payload.edited_text:
        raise HTTPException(status_code=422, detail="edited_text is required for edit")
    if payload.decision == "approve" and payload.edited_text:
        raise HTTPException(status_code=422, detail="use edit when changing reply text")
    decision = "approved" if payload.decision in {"approve", "edit"} else "rejected"

    def handle() -> tuple[int, Any]:
        try:
            if kind == "send_reply":
                approval_id = record_reply_decision(
                    pool, ticket_id=str(ref), decided_by=operator.strip(),
                    decision=decision, edited_text=payload.edited_text, note=payload.note,
                )
                enqueue(
                    pool, str(ref), "resume_agent",
                    {"approved": decision == "approved", "decided_by": operator.strip(),
                     "edited_text": payload.edited_text, "approval_id": approval_id},
                    f"resume:{ref}:{approval_id}",
                )
            elif kind == "refund":
                approval_id = decide_refund(pool, str(ref), operator.strip(), decision, payload.note)
                if decision == "approved":
                    execute_refund(pool, request_id=str(ref), approved_by=operator.strip())
            elif kind == "plan_change":
                approval_id = decide_plan_change(pool, str(ref), operator.strip(), decision, payload.note)
                if decision == "approved":
                    execute_plan_change(pool, request_id=str(ref), approved_by=operator.strip())
            else:
                raise ToolFailure(ToolErrorCode.INVALID_INPUT, "unknown approval kind")
        except ToolFailure as exc:
            raise _failure(exc) from exc
        return 202, {"approval_id": approval_id, "status": "accepted"}

    try:
        _status, body, _replay = execute_once(
            pool, key=idempotency_key, endpoint=f"POST:/api/approvals/{kind}/{ref}/decide",
            body=payload.model_dump(), handler=handle,
        )
    except ToolFailure as exc:
        raise _failure(exc) from exc
    return DecisionAccepted.model_validate(body)
