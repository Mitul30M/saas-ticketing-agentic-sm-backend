# Phase 4 — API, approval service, idempotency, tracing (Day 3)

> Source: `PLAN.md` Part 7, Phase 4.

## II. Checklist

- [ ] FastAPI app, routers per 4.9; Pydantic request/response models; CORS for web origin; agent-token auth dependency for dashboard routes.
- [ ] Background execution of agent runs (BackgroundTasks or jobs-table worker); status transitions persisted.
- [ ] `services/idempotency.py`: dependency/middleware for `Idempotency-Key` (replay, mismatch→422, concurrency-safe via unique insert).
- [ ] `services/approvals.py` + `services/executor.py`: decide endpoint → lock row → verify `pending_approval` → write `approvals` + `audit_log` → resume graph via `Command(resume=…)` → deterministic executor performs refund (updates `payments.amount_refunded_cents`, invoice status) in one transaction; duplicate decisions are no-ops; `send_reply` writes to an `outbox` table (simulated email).
- [ ] `services/tracing.py`: decorators for nodes/tools/LLM calls → `trace_events` with tokens, cost (from `config/pricing.yaml`), cache_hit, duration.
- [ ] `/api/metrics/summary` and `/api/tickets/{id}/trace`.
- [ ] Integration tests: replay same POST 5× → 1 ticket; two concurrent approvals → exactly one execution; approve → refund executed once; reject → no data change.

## Definition of Done (verify with commands)

Via `curl` / OpenAPI docs: submit ticket → agent pauses at approval → approve → refund visible in DB **once**, `audit_log` row present.

## Notes & key facts

- **API surface (PLAN §4.9):** `POST /api/tickets` (Idempotency-Key), `GET /api/tickets` (filters), `GET /api/tickets/{id}`, `GET /api/tickets/{id}/trace`, `POST /api/tickets/{id}/run`, `POST /api/approvals/{action_id}/decide`, `GET /api/approvals/pending`, `GET /api/customers/{id}`, `GET /api/kb/*`, `GET /api/metrics/summary`, dev-only `/api/admin/*`, `GET /healthz`. Changing public API shapes also requires updating the separate frontend repo's types + PLAN §4.9.
- **Idempotency — three layers (PLAN §4.6), the highlight feature:**
  1. API layer: `Idempotency-Key` table; same key+body → replay stored response; same key+different body → `422`; ticket dedupe is **soft and windowed** — migration 006 dropped the global `tickets.content_hash` UNIQUE (it swallowed legitimate identical follow-ups), intake collapses same `from_email` + `content_hash` within `intake_dedupe_window_minutes` (see P4-3).
  2. Business layer: `refund_requests` `unique(invoice_id, idempotency_key)`; partial unique index blocks a second *open* refund per invoice; executor `SELECT … FOR UPDATE`, checks `status='approved'` before moving to `executed` in ONE transaction; duplicate approvals are no-ops.
  3. LLM/cost layer: `llm_cache` sha256(provider|model|params|prompt) → re-running identical inputs costs $0; cache hit-rate reported in cost section.
- **Only `services/approvals.py` + `services/executor.py` may move rows to `approved`/`executed`**, and only after a recorded human decision. Never a code path where the agent/graph calls the executor directly.
- Layers: `api/` (HTTP only) → `services/` (business logic) → tools/llm/db helpers; services raise domain exceptions mapped to HTTP codes in `api/`.
- Agent runs in background worker — never block the POST request.
- Append-only `audit_log`; structured JSON logs with `trace_id` and `ticket_id`; never log secrets.

## Log & achievements (append)
