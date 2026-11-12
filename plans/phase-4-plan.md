# Phase 4 — API, approval service, idempotency, tracing (Day 3)

## Current implementation status

- P4-1 ticket-state projection, P4-2 idempotency, P4-3 intake/jobs foundations,
  P4-5 API/auth/CORS routes, and P4-6 cost/metrics tracing are implemented.
- P4-7 safety acceptance coverage is implemented: database approval bypass,
  rejection/no-money-movement, concurrent replay, not-in-KB escalation,
  injection blocking, typed tool failure, and step-limit escalation.
- Migration `009_plan_change_schedule.sql` adds deferred plan-change fields.
- The full Phase 4 DoD remains open until the API-worker approval flow and
  executor rollback/API replay tests are added and run.

Verification so far: `make lint` passes, `make test-all` reports 232 passed,
`make migrate` applies migration 009, and API smoke test returns health 200
while protected routes fail closed with 503 when `AGENT_TOKEN` is unset.

> Source: `PLAN.md` Part 7, Phase 4.

## II. Checklist

- [x] FastAPI app, routers per 4.9; Pydantic request/response models; CORS for web origin; agent-token auth dependency for dashboard routes.
- [x] Background execution of agent runs (BackgroundTasks or jobs-table worker); status transitions persisted.
- [x] `services/idempotency.py`: dependency/middleware for `Idempotency-Key` (replay, mismatch→422, concurrency-safe via unique insert).
- [x] `services/approvals.py` + `services/executor.py`: decide endpoint → lock row → verify `pending_approval` → write `approvals` + `audit_log` → resume graph via `Command(resume=…)` → deterministic executor performs refund (updates `payments.amount_refunded_cents`, invoice status) in one transaction; duplicate decisions are no-ops; `send_reply` writes to an `outbox` table (simulated email). ✅ Service and database safety tests pass.
- [x] `services/tracing.py`: decorators for nodes/tools/LLM calls → `trace_events` with tokens, cost (from `config/pricing.yaml`), cache_hit, duration.
- [x] `/api/metrics/summary` and `/api/tickets/{id}/trace`.
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

- 2026-11-12: Added `tests/safety/` AT1–AT5 coverage. Full validation: `make test-all` — 240 passed; `make lint` — ruff and mypy clean.
