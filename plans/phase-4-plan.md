# Phase 4 — API, approval service, idempotency, tracing (Day 3)

> Source: `PLAN.md` Part 7, Phase 4.

## II. Checklist

- [x] FastAPI app, routers per 4.9; Pydantic request/response models; CORS for web origin; agent-token auth dependency for dashboard routes.
- [x] Background execution of agent runs via the `jobs` table (worker process, `FOR UPDATE SKIP LOCKED`, stale-lock reclaim, `max_attempts`, `dedupe_key`); status transitions persisted by `services/ticket_state.py`.
- [x] `services/idempotency.py`: `Idempotency-Key` claim (replay, mismatch→422, in-flight→409+Retry-After, concurrency-safe via unique insert); expiry cleanup on a worker tick.
- [x] `services/approvals.py` + `services/executor.py`: decide endpoint → lock row → verify `pending_approval` → write `approvals` + `audit_log` → resume graph via `Command(resume=…)` (reply) → deterministic executors for refund/plan-change/reply; duplicate decisions are no-ops; `send_reply` writes to `outbox`.
- [x] `services/tracing.py`: node/tool/LLM trace rows with tokens, cost (`config/pricing.yaml`), cache_hit, duration.
- [x] `/api/metrics/summary` and `/api/tickets/{id}/trace`.
- [x] Integration tests: replay same POST 5× → 1 ticket; two concurrent approvals → exactly one execution; approve → refund executed once; reject → no data change.

## Definition of Done (verify with commands)

Via `curl` / OpenAPI docs: submit ticket → agent pauses at approval → approve → refund visible in DB **once**, `audit_log` row present.

## Notes & key facts

- **API surface (PLAN §4.9):** `POST /api/tickets` (Idempotency-Key), `GET /api/tickets` (filters), `GET /api/tickets/{id}`, `GET /api/tickets/{id}/trace`, `POST /api/tickets/{id}/run`, `POST /api/approvals/{kind}/{ref}/decide`, `GET /api/approvals/pending`, `GET /api/customers/{id}`, `GET /api/kb/*`, `GET /api/metrics/summary`, dev-only `/api/admin/*`, `GET /healthz`. Changing public API shapes also requires updating the separate frontend repo's types + PLAN §4.9.
- **Idempotency — three layers (PLAN §4.6), the highlight feature:**
  1. API layer: `Idempotency-Key` table; same key+body → replay stored response; same key+different body → `422`; ticket dedupe is **soft and windowed** — migration 006 dropped the global `tickets.content_hash` UNIQUE (it swallowed legitimate identical follow-ups), intake collapses same `from_email` + `content_hash` within `intake_dedupe_window_minutes` (advisory-locked, see P4-3).
  2. Business layer: `refund_requests` `unique(invoice_id, idempotency_key)`; partial unique index blocks a second *open* refund per invoice; executor `SELECT … FOR UPDATE`, checks `status='approved'` before moving to `executed` in ONE transaction; duplicate approvals are no-ops; reply decisions are run-scoped (migration 012).
  3. LLM/cost layer: `llm_cache` sha256(provider|model|params|prompt) → re-running identical inputs costs $0; cache hit-rate reported in cost section.
- **Only `services/approvals.py` + `services/executor.py` may move rows to `approved`/`executed`**, and only after a recorded human decision. Never a code path where the agent/graph calls the executor directly.
- Layers: `api/` (HTTP only) → `services/` (business logic) → tools/llm/db helpers; services raise domain exceptions mapped to HTTP codes in `api/`.
- Agent runs in background worker — never block the POST request.
- Append-only `audit_log`; structured JSON logs with `trace_id` and `ticket_id`; never log secrets.

## Log & achievements (append)

### 2026-11-12 · Implementation review round (evidence + hardening)

**Hardening after review (all covered by tests):**
- Reply decisions now bind to the **run** (`approvals.run_key`, migration 012), so an
  identical re-run draft can be decided again; replays after resolution return the
  recorded decision instead of 409.
- Stale `running` jobs at `max_attempts` are terminated (ticket parked `awaiting_human`,
  `JOB_FAILED`, audit row) instead of stuck; `enqueue` reuses done dedupe keys and
  resets failed ones; `enqueue_rerun` goes through the status guard (`resolved → triaging`).
- Idempotency stale-claim reclaim is conditional (no double handler run); the request-size
  limit checks `Content-Length` before buffering.
- Deferred plan changes are applied by the worker tick (`apply_due_plan_changes`,
  `subscriptions.scheduled_plan_change_id`); admin routes are actually mounted behind
  `ENABLE_ADMIN_ROUTES`; ticket detail returns `flags` + `proposed_actions` (the previous
  query was unreachable); shadow price names its reference model.

**Test evidence (this round):**
- New suites: `tests/integration/test_jobs.py` (dedupe, stale claim, terminal failure,
  rerun guard), `test_idempotency.py` (replay, mismatch, concurrency, claim release,
  expiry), `test_plan_change.py` (approve/reject, next_cycle scheduling + apply),
  `test_phase4_resume.py` (executor retry sends once, run-scoped drafts, replay key).
- Rewritten `tests/integration/test_api_http.py`: auth matrix over every dashboard route,
  header ordering, edit/approve validation on a real draft, 413 (Content-Length and
  chunked), replay 5× → one ticket, full submit → approve → one outbox row, metrics.
- `tests/safety/test_phase4_safety.py`: AT2–AT5 now run through the real
  API/job/graph path (escalation parked, injection flagged and unable to write, tool
  ownership failure traced, step limit persisted).
- `make test-all`: **282 passed**; `make lint`: clean.

**Clean-compose walkthrough:** recorded in `docs/DECISIONS.md` / this log after running
`docker compose down -v` + `AGENT_TOKEN=… docker compose up -d --build`, `make seed`,
`make ingest-kb`, then curl + psql. Stub mode cannot choose the refund branch (its triage
is fixed to `invoice_question`); the refund approval is therefore demonstrated via the
`propose_refund` tool call (the agent's own proposal path) followed by the HTTP decide
endpoint, and the reply path is demonstrated end-to-end from ticket submission.
