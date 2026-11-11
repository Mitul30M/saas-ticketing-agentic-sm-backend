# DECISIONS.md — non-obvious design decisions (what / why / alternatives)

Format: 3–5 lines per decision. Newest at the bottom.

---

### 2026-11-10 · Backend-only repo split (`app/` at root, Next.js separate)
- PLAN.md originally assumed a monorepo with `backend/` + `web/`. The owner keeps the
  Next.js frontend in a separate repository, so this repo holds only the FastAPI backend.
- Effect: `backend/app/` → `app/`, `backend/tests/` → `tests/`; imports unchanged (`app.*`).
- Alternatives: monorepo (rejected: owner preference, smaller assessment repo).

### 2026-11-10 · Python 3.14 via `uv` (not 3.12)
- PLAN.md said 3.12; the owner's machine ships 3.14 (Homebrew) + uv 0.11.25, and the
  reference repo already runs 3.14. Locked as `requires-python = ">=3.14"`.

### 2026-11-10 · Migration runner: file-based with `schema_migrations` bookkeeping
- Mechanism copied from the reference repo (ordered migrations, one transaction each,
  `schema_migrations` table, `IF NOT EXISTS` DDL), except SQL lives in numbered
  `db/migrations/*.sql` files (PLAN 4.12) instead of inline Python strings.
- Alternatives: inline Python strings (reference default; rejected — SQL files review
  better), Alembic (rejected: heavier than needed).

### 2026-11-10 · DB image: `pgvector/pgvector:pg16`, host port 5433
- pgvector extension ships in this image; plain `postgres:16-alpine` (reference default)
  would need manual extension installation.
- Host port 5433 (reference pattern) avoids collisions with any local Postgres.

### 2026-11-10 · LLM provider: Groq via `langchain-groq` ChatGroq (reference parity)
- Initial PLAN listed `mock | ollama | openai_compat`. Owner decided: **Groq**, matching
  the reference project's `ChatGroq` pattern (settings-driven, temp 0, bounded retries,
  repair-once JSON, deterministic fallback). `stub` remains provider value for no-LLM runs.
- `LLMClient` abstraction stays as the only call path (AGENTS.md rule); groq is a provider
  inside it. API key/URL/model configurable via env like the reference.

### 2026-11-10 · Observability: `trace(conn, ticket_id, trace_id, node, kind, ...)` helper
- Trace style is the **same shape as the reference** single-helper design: one function,
  JSONB input/output serialized with `default=str`, one row per node/tool/LLM step,
  readable via an endpoint + UI. Table renamed/extended to `trace_events` per PLAN 4.8
  (adds tokens/cost/cache_hit from day one).

### 2026-11-10 · Safety triggers live in migrations, tested against real DDL
- Refund state machine guard and `audit_log` append-only trigger are DB triggers in
  `003_support.sql`; the integration suite exercises them against the running Postgres
  (skip-if-down) instead of mocking.
- Alternatives: enforce only in service code (rejected — agent inserts could bypass).

### 2026-11-10 · Reference repo: QuickDrop-Rider-Payment-HelpDesk-Agent (READ-ONLY)
- `/Volumes/Mitul/Projects/QuickDrop-Rider-Payment-HelpDesk-Agent` is the owner's prior project and the
  baseline for patterns: DB claims/idempotency, `trace()` single-helper observability, ops approval queue
  with CAS + re-verify-from-source, bounded LLM client (Groq/ChatGroq), pytest integration style.
- Policy: consult freely for structuring/wiring agent+LLM work, but **never write to it**.

### 2026-11-10 · Dataset reality check (owner-confirmed)
- `Tobi-Bueck/customer-support-tickets` is a tickets table only — no customers/payments/orders/KB.
- Consequence: seed the business DB first, then link tickets to it; inject invoice refs into billing
  bodies; "order status" tool maps to subscription+invoice status; payments table carries the exact
  fields refund tools must check. Requirements captures live in PLAN §4.2.1 "Reality check".

### 2026-11-11 · Deterministic business clock and database approval gate
- Seed and policy-time calculations use `AS_OF` (default `2026-11-10T12:00:00Z`) rather than the
  wall clock, so refund-window labels and demo data do not change between runs.
- Migration 006 makes sender identity explicit and requires a recorded human approval plus timestamps
  before refund or plan-change state transitions can reach `approved` or `executed`. Requirements captures live in PLAN §4.2.1 "Reality check".

### 2026-11-11 · KB coverage is explicit and conservative
- The 30-article KB covers common billing, plan, access, API, sync, refund, and limited outage questions.
- Security incidents, feature requests, physical-document requests, and incident-specific outage claims
  remain intentionally uncovered so the agent escalates instead of guessing.
- `COVERAGE.md` is the labeling source for retrieval/evaluation; near-duplicate refund and outage
  articles make recency and evidence boundaries testable.

### 2026-11-11 · Shared contracts precede provider and tool wiring
- Agent code receives one `LLMClient` interface and Pydantic tool contracts, so vendor
  responses and untrusted model arguments cannot leak directly into business services.
- Taxonomy-to-queue routing is deterministic code rather than an LLM instruction.
- Full cache persistence, retry/repair, and registry execution stay as separate steps so
  each safety boundary can be tested independently.

### 2026-11-11 · Normalize vendor failures at the LLM boundary
- The Groq adapter catches provider exceptions at the vendor boundary and converts them
  into `LLMError`, so graph code never depends on vendor-specific exception classes.
- The deterministic stub fills required schema fields by annotation rather than relying
  on optional defaults, keeping CI behavior representative of real structured calls.

### 2026-11-11 · Stub structured output: `stub_example()` convention
- Schemas handed to `LLMClient.generate(schema=...)` should define a
  `stub_example()` classmethod returning a realistic dict; the stub uses it
  verbatim (deterministic, valid for Literal/nested/bounded fields).
- Schemas WITHOUT an example go through the naive annotation filler plus a
  self-validation pass; anything it cannot satisfy raises `LLMError` in CI
  instead of silently passing bad data to the graph.
- Alternative considered: making the generic filler understand Literal,
  bounds, and nested models — rejected as clever; examples are explainable
  and double as documentation.
- Loader (`scripts/load_tickets.py`) skips duplicates by
  `(from_email, content_hash)` because migration 006 replaced the global
  unique hash with soft, windowed intake dedupe.
