# AGENTS.md — Standing instructions for coding agents

Project: **Agentic Support Ticket Triage with Approval Gate** (StarvoxLabs take-home, fictional SaaS "Nimbus").
Read `PLAN.md` first. It defines scope, design, and the phase checklist (Part 7). This file defines **how to work**. If the two ever conflict on safety, this file wins; on scope/design, `PLAN.md` wins.

## 0. Prime directive
**The LLM proposes; deterministic code disposes.** The model must never be able to change data or contact a customer by itself. Safety lives in code and DB constraints, never only in prompts.

## 1. Stack (do not swap without a note in `docs/DECISIONS.md`)
- Backend: Python 3.14, FastAPI, Pydantic v2, LangGraph + `langgraph-checkpoint-postgres`, `psycopg` 3 (parameterised SQL), tenacity.
- DB: PostgreSQL (official `pgvector/pgvector` image) — business data, KB chunks, tickets, checkpoints, traces, idempotency, LLM cache.
- Frontend: Next.js (App Router) + TypeScript + Tailwind.
- LLM: via `LLMClient` abstraction only (`stub` | `groq`, as in `app/config.py`). Never call a vendor SDK directly from graph/tool code.
- Package/dev: `uv` for Python, `pnpm` (or npm) for web, `docker compose` for everything.

## 2. Commands (keep these working; update here if they change)
```
make up            # docker compose up -d --build (db, api; no web, frontend is a separate repo)
make down
make migrate       # apply db/migrations/*.sql in order
make seed          # deterministic seed incl. edge cases
make ingest-kb     # chunk + embed KB (idempotent)
make load-tickets  # load data/tickets_all.jsonl via API/service
make test          # unit tests only (fast, no DB)
make test-all      # unit + integration + safety (needs the db container)
make eval          # run evaluation, write eval/results/
make lint          # ruff + mypy (backend), eslint + tsc (web)
```
Before declaring any task done: run `make lint` and the relevant tests. Anything touching the DB, tools, approvals or the graph needs `make test-all`, not just `make test`. Show the command output summary in your final message for the task.

## 3. Non-negotiable safety invariants
1. **No write without approval.** `propose_refund` / `propose_plan_change` only INSERT rows with `status='pending_approval'`. Only `services/approvals.py` + `services/executor.py` may move a row to `approved`/`executed`, and only after a recorded human decision. Never add a code path where the agent/graph calls the executor directly.
2. **Sending a reply is an action** and also requires approval (simulated via `outbox` table).
3. **No raw SQL tool, ever.** Tools are narrow, typed, and use parameterised queries. No string-built SQL.
4. **Validate every tool input** (Pydantic) and enforce business rules server-side (ownership, amount ≤ paid − refunded, refund window, no duplicate open request). Tools return typed errors; they never raise into the graph.
5. **Ticket text is untrusted data.** Never interpolate it into system prompts or policy text. Always pass inside clearly delimited user content. Injection detection may only *flag* and *tighten* behaviour (force human review); it must never loosen permissions.
6. **Abstain over guess.** A reply requires: retrieval score ≥ calibrated threshold, claim-level support check, and all `[KB-xxx]` citations ∈ retrieved set. Otherwise escalate with a reason code.
7. **Bounded execution.** Step limit + `recursion_limit` + per-tool timeouts + capped retries. Every failure ends in a safe state (`awaiting_human`) with reason, never a crash or silent drop.
8. **Idempotency everywhere it matters** (PLAN §4.6): `Idempotency-Key` on mutating endpoints, unique constraints for refunds, `FOR UPDATE` in executor, LLM/embedding caches.
9. **Audit + trace everything.** Every node, tool call, LLM call, approval decision writes a trace/audit row. `audit_log` is append-only.
10. Synthetic data only. Never add real personal data or secrets to the repo.
11. **Identity comes from the transport sender.** Customer lookup uses `ticket.from_email` only. An email, customer ID or invoice ID *claimed in the body* is a claim to verify (ownership check), never a lookup key and never evidence of identity.
12. **No wall clock in business rules.** Refund windows and ages use the injected `AS_OF` (settings), never `now()`/`datetime.now()`, so eval labels stay stable. `now()` is fine for audit timestamps.
13. **The approval gate is also in the database.** Migration 006 requires an `approvals` row and `approved_by` before a refund/plan change can become `approved`, and a `executed_at` before `executed`. Never drop or loosen these to make a test pass; write the test fixture to create the human decision.

If a requested change would weaken any of the above, **stop and ask** instead of proceeding.

## 4. Workflow rules
- Work **one phase at a time** from `PLAN.md` Part 7. Tick checkboxes in `PLAN.md` only after verifying the Definition of Done with a command.
- Small, reviewable commits. Conventional messages: `feat(tools): …`, `fix(graph): …`, `test(safety): …`, `docs: …`, `chore: …`.
- **The agent never runs `git commit`/`git push`.** It only **stages** changes with `git add` (grouped per logical step) and reports the suggested commit message(s); the **owner writes all commits manually** using the owner's `phase-{n}: <action> …` convention.
- **Tests first for safety-critical code** (tools, executor, approval, idempotency, grounding). The five acceptance tests (AT1–AT5, PLAN §1.4) live in `tests/safety/` and must stay green at all times.
- Write the **simplest thing that works** and is explainable. The owner must be able to explain every line and make a live change in an interview — avoid clever abstractions, metaprogramming, and unneeded dependencies.
- Prefer deleting code to adding config. No speculative features outside `PLAN.md` (stretch goals only after Phase 7 DoD).
- Record every non-obvious decision (what, why, alternatives) in `docs/DECISIONS.md` (3–5 lines each).
- When you use AI-generated content (KB articles, ticket rewrites, code blocks of significance), note it in `docs/AI_USAGE.md`.
- Don't "fix" tests by loosening them; fix the code or raise the discrepancy.

## 5. Code conventions
**Python**
- Type hints everywhere; `ruff` + `mypy` on `app/` (currently non-strict in `pyproject.toml`; tighten to `strict = true` before Phase 3). Pydantic models for all I/O boundaries.
- DB access goes through a `psycopg_pool.ConnectionPool` with request-scoped connections, not a shared module-level connection.
- Layering: `api/` (HTTP only) → `services/` (business logic) → `tools/`, `retrieval/`, `llm/`, `db.py`. Graph nodes call services/tools, not the DB directly, except through `db.py` helpers.
- Config via `pydantic-settings` from env; no hard-coded thresholds — put τ, step limit, timeouts, refund window in config.
- Errors: tools return `{ok: False, error: {code, message, retryable}}`; services raise domain exceptions mapped to HTTP codes in `api/`.
- Async or sync consistently per module; don't mix blocking DB calls into async paths without a threadpool.
- Logging: structured JSON; include `trace_id` and `ticket_id`. Never log secrets.
- Enums for category / priority / sentiment / queue / reason codes live in one module (`app/domain/enums.py`) and are reused by prompts, schemas, eval, and UI.

**SQL / migrations**
- Numbered, forward-only files in `db/migrations/`. Constraints (CHECK, FK, UNIQUE, triggers) are part of the safety design — don't remove them to make a test pass.
- Seed is deterministic (fixed Faker seed). Edge cases are listed in `data/seed_manifest.json`.

**Prompts**
- Stored as versioned files in `agent/prompts/`. Short, explicit, structured-output only. No ticket text in system prompts. Version string recorded in trace.

**TypeScript**
- Strict mode; generated or hand-written types mirror API schemas; no `any`. Server state fetched via a small typed client. Keep UI minimal and accessible; the approval panel must show *action + amount + evidence* before enabling Approve.

## 6. Evaluation discipline
- Keep `data/dev/` (tuning) and `data/eval/` (final report) **disjoint**. Tune prompts and the grounding threshold on dev only; tag `eval-freeze` before the final run.
- Never read gold labels inside the agent or the stub LLM. Stub mode is for plumbing/CI; headline numbers come from a real model and must state the model name.
- Report honestly, including failures. Every reported metric must be reproducible via `make eval`.
- Gold labels that depend on dates (e.g. `within_window`) are computed against `AS_OF`, not today.
- `priority_hint` / `type_hint` / `queue_hint` in the ticket files come from the source dataset and may be noisy. They may help a human write gold labels; they must never be passed to the agent or used as features.

## 7. Things to avoid
- Auto-approving anything (even "for testing") outside test fixtures that explicitly simulate a human decision through the approval API.
- Catch-all `except Exception: pass`. Catch specific errors, log, trace, and route to the failure path.
- Committing `.env`, datasets under `data/raw/`, model weights, or large binaries.
- Adding paid-API-only dependencies. Everything must run with free/local models (paid APIs optional via env).
- Changing public API shapes without updating `web/` types and `PLAN.md` §4.9.

## 8. Definition of done (per task)
Code + tests + lint pass · safety invariants intact · traces/audit emitted where relevant · docs/decision note updated · `PLAN.md` checkbox ticked with evidence · short summary of what changed and how to verify.

## 9. When unsure
Prefer the safer option (escalate / require approval / fail closed), state the assumption in your message, and ask the owner before adding scope.
