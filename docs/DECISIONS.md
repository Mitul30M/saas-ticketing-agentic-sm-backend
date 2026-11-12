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

### 2026-11-11 · Graph approvals are proposal-only
- The LangGraph approval interrupt collects the human decision and resumes to an
  `action_proposed` state; it does not write `outbox` or perform customer-visible work.
- Approval recording and execution are separate service calls that write an audit row,
  lock and re-verify the decision, and make the outbox insert replay-safe.
- The graph may propose a validated refund or plan change after retrieving
  customer/billing evidence and grounded KB evidence, but it never approves or
  executes the action. The human-facing approval/API layer owns every side effect.

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

### 2026-11-11 · Registry writes the trace row, never the handler
- The tool registry executes handlers on a small ThreadPoolExecutor with a
  per-call timeout; after a timeout the handler thread is abandoned and may
  still finish — which is why the registry (not the handler) writes the single
  trace row. A tracing handler could add a second row after a timeout.
- Handlers receive a VALIDATED Pydantic model, so they cannot read unvalidated
  keys; validation errors report field names only, never rejected values
  (attacker text must not flow back to the model).

### 2026-11-11 · Ownership semantics: sender-resolved, anti-probe, fail-closed
- Identity is resolved from `tickets.from_email` in the DB; `from_email` in
  tool arguments is optional and ignored (invariant 11), so the model cannot
  impersonate by claiming an email.
- `get_invoice` returns the same `OWNERSHIP_MISMATCH` for a missing and a
  foreign invoice so callers cannot probe which invoice ids exist. Ambiguous
  duplicate-email senders fail closed with `NOT_FOUND` (escalate).
- `propose_refund` stores a SERVER-derived idempotency key
  (`sha256(refund|ticket|invoice|amount)`); the agent-supplied key is kept as
  evidence metadata only, so replays dedupe and altered amounts never collide.

### 2026-11-11 · LLM cache + repair-once in `generate()`
- `LLMClient(pool=...)` enables the `llm_cache` layer (identical requests
  answered with `cache_hit=True` and stored token counts); without a pool no
  caching, keeping pure unit usage deterministic.
- Real-provider JSON failures get ONE bounded repair: strip code fences, then
  a single re-ask with a strict instruction; the stub path never repairs
  (deterministic output that fails self-validation is a plumbing bug and must
  raise `LLMError`).

### 2026-11-11 · Hybrid retrieval: RRF k=60, rerank off by default
- `hybrid_search` runs FTS (`websearch_to_tsquery` — injection-safe by design,
  query text only ever a bind parameter) and pgvector cosine in parallel and
  fuses with Reciprocal Rank Fusion k=60 (PLAN 4.4); both channels' ranks are
  kept on the result so grounding can see WHY a chunk surfaced.
- Cross-encoder rerank ships behind `RETRIEVAL_RERANK_MODEL` (empty = off);
  headline eval numbers must state whether it was on.

### 2026-11-11 · Triage: prompt proposes, code disposes (queue)
- `triage_ticket` emits `{category, priority, sentiment, reasons}` via one
  structured call; the queue is mapped in code through `CATEGORY_QUEUE`
  (app/domain/enums.py) — the model never picks the queue (PLAN 4.5).
- Prompt `triage_v1.txt` is static and versioned; ticket text enters ONLY the
  user message inside `<<<SUBJECT>>> / <<<BODY>>>` delimiters, and the system
  prompt states the injection rule. A guard in `_system_prompt` refuses a
  system prompt that contains the delimiters.

### 2026-11-11 · plan_change evidence lives in the trace
- `plan_change_requests` has no evidence column; the validated proposal's
  kb_ids and claimed key are persisted in the tool trace row (registry writes
  it every call), keeping the audit trail complete without weakening the
  proposal schema.

### 2026-11-11 · Version prompts when structured gates change
- Grounding and draft prompt versions are bumped when required claims or
  citations change.
- This invalidates old LLM cache entries whose structured output may satisfy
  previous schema semantics but fail the current deterministic gates.

### 2026-11-12 · Shared-token dashboard identity
- Dashboard routes require one configured `AGENT_TOKEN`; the required
  `X-Operator` value is stored as the human audit label.
- It is not person-level authentication, which is a known limitation until
  a real identity provider is added.

### 2026-11-12 · P3-1/P3-2: strict decision parsing; executor derives the sent text
- The interrupt resume value authorizes ONLY when it parses as
  `{approved: True (literal), decided_by: non-blank str}`; anything else
  (`"true"`, `1`, missing decided_by) is a recorded rejection with
  `decided_by="invalid_resume_payload"` — fail closed (P3-1).
- `execute_approved_reply` no longer accepts a `body` argument: the sent text
  is the approval's `edited_text` when the human edited, else
  `tickets.draft_reply` (persisted by the draft node before the gate). The
  text that is sent must be the text the human saw — a caller-supplied body
  could diverge silently (P3-2).

### 2026-11-12 · P3-5: the graph gate approves the REPLY only
- When the agent also proposes a refund/plan change, the interrupt payload
  labels the approval `kind: send_reply` vs the action; a rejected draft does
  NOT auto-reject the pending proposal. Refund/plan-change decisions get
  their own approvals rows (`action_type='refund'|'plan_change'`) and their
  own executor path in Phase 4 — the dashboard shows proposals separately.
- Rationale: one gate approving two things at once is how a UI shows one
  thing and approves another. Rejected proposals stay visible for a human
  instead of disappearing.

### 2026-11-12 · FTS channel: OR-combine sanitized tokens
- `websearch_to_tsquery` defaults to AND, so a full natural sentence ("How
  does the refund policy work?") matched NOTHING and hybrid retrieval
  degenerated to vector-only. `fts_search` now OR-combines `[A-Za-z0-9_]+`
  tokens (still bind parameters), restoring recall for real ticket language.
- Score gate unchanged (grounding_threshold 0.02): single-channel rank-1 is
  1/(60+1)≈0.0164 (fails), two-channel agreement ≥~0.03 (passes). Real
  calibration happens on data/dev only (AGENTS §6).

### 2026-11-12 · Amount/plan parsers are pure and ambiguous→escalate
- `parse_amount_cents` / `parse_target_plan` (orchestration.py) raise
  ValueError on ambiguity ("$50 and $75", "from Pro to Starter, not
  Business"); the node converts that into NEEDS_HUMAN_JUDGEMENT. Directional
  from/to readings decide only when they account for every mentioned plan.
- `propose_refund` now returns BUSINESS_RULE (not OWNERSHIP_MISMATCH) for an
  owned invoice without a succeeded payment, via LEFT JOIN + max(paid_at).

### 2026-11-12 · Grounding threshold evidence (P4-0.3, reproducible)
- **Command:** `make grounding-report` → `eval/results/grounding_dev.csv`
  (script: `scripts/grounding_report.py`). One row per dev ticket: hand label
  `kb_covered` (from `data/kb/COVERAGE.md`), best RRF, top chunk, per-channel
  ranks, channel agreement; with `LLM_PROVIDER=groq` also the real support
  verdict. Never hand-edit the CSV; regenerate it.
- Finding after the OR-FTS change (P3-6): the score gate now mostly filters
  **gibberish** — every real dev ticket passes it (best RRF 0.0309–0.0328,
  both channels agreeing on the top chunk), while a gibberish negative
  control scores 0.0164 (single channel). Before the OR-FTS fix, several
  topics (security incident, feature request, fax/invoice) were vector-only
  and could fall below the gate.
- **Honest limitation:** whether retrieved chunks actually ANSWER a ticket
  is decided by the LLM support check, which so far is only tested with
  scripted responses. Not-in-KB behaviour (uncovered dev topics escalating)
  has not been measured with a real model yet. The threshold τ=0.02 is
  **NOT calibrated** — no calibration claim until the real-model run in the
  evaluation phase measures it (AGENTS §6: tune on data/dev only, freeze
  before eval).


### 2026-11-12 · Promise-language deny-list widened (P4-0.2)
- Added future-tense commitment patterns (we will/we'll/I will/going to +
  process|issue|send|initiate|credit|return|refund; you will/you'll +
  receive/get + refund; initiated/started/submitted a refund) to
  `app/agent/nodes/draft.py` PROMISE_PATTERNS.
- **Defense in depth, NOT the control.** The human approval gate is the
  control: an approved reply is the human's recorded decision, and edited
  promise language is flagged (`edited_promise_language`), never blocked.
  The deny-list only stops the *model* from reaching the gate with a
  committed refund/plan/action promise in its own draft.
- False-positive guard: `tests/unit/test_promise_denylist.py` scans every
  sentence of every `data/kb/*.md` article — real KB text trips nothing.
  Alternatives rejected: LLM-based promise detection (nondeterministic,
  defeats the purpose), blocking edited human text (overrides human authority).
