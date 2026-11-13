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

### 2026-11-11 · plan_change evidence is duplicated for review
- The validated proposal's `kb_ids` remains in the tool trace and is now also
  copied into `plan_change_requests.evidence` for the pending-approval console.
- Keeping both records preserves the detailed trace while making the approval
  decision self-contained and queryable.

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

### 2026-11-12 · Reply approvals bind to a draft snapshot
- Each reply decision stores the SHA-256 hash of the draft shown to the operator
  and snapshots the approved text in `approvals.edited_text`.
- A ticket may therefore be re-run after rejection without reusing an old decision,
  while execution remains deterministic if the ticket projection changes later.

### 2026-11-12 · Agent runs use fresh checkpoint threads
- Initial and rerun jobs use `ticket:{id}:run:{number}` and persist that thread on
  the ticket; resume jobs read the persisted value rather than recomputing it.
- This prevents step counters and escalation state from leaking across reruns.

### 2026-11-12 · Intake and dashboard limitations
- Public ticket intake remains intentionally unauthenticated and is recorded as a
  known rate-limit limitation; the configured shared dashboard token is not identity.
- Pending approvals expose reply drafts, flags, citations, and action evidence so a
  human can review the exact proposed customer-visible operation.

### 2026-11-12 · Reply decisions are run-scoped, not draft-only
- Migration 012 adds `approvals.run_key` (the ticket's checkpoint thread) and
  replaces the draft-only unique index with `(action_type, action_ref, run_key,
  draft_hash)`. A re-run that produces identical text (the LLM cache makes it
  likely) is a NEW decision; a replay inside one run stays idempotent.
- `record_reply_decision` checks for an existing decision before requiring
  `awaiting_approval`, so replaying the same decision after the ticket resolved
  returns the recorded approval instead of a spurious 409. A *different*
  decision for the same draft is still DUPLICATE.
- Rejected alternative: hashing draft+thread into one string (opaque in audit
  queries); keep the two components separate.

### 2026-11-12 · Job queue failure and replay semantics
- `enqueue` returns the existing job for a done dedupe key (a decision replay
  after the resume job finished must not 409) and resets a *failed* job to
  queued so an operator retry can progress.
- `claim` reclaims running rows past `job_lock_timeout_s`; if such a row is
  already at `max_attempts` it is failed terminally (ticket → `awaiting_human`,
  `escalation_reason=JOB_FAILED`, audit row) instead of staying `running` forever.
- `enqueue_rerun` moves the ticket through the guarded state machine
  (`resolved → triaging` is an explicit operator re-run edge), never a raw UPDATE.
- Idempotency stale-claim reclamation is a conditional UPDATE (`... RETURNING`),
  so two stale reclaimers cannot both run the handler.

### 2026-11-12 · Deferred plan changes need a worker tick
- `execute_plan_change` records `subscriptions.scheduled_plan_change_id`
  (migration 012) when `effective=next_cycle`; the worker applies due rows with
  `FOR UPDATE SKIP LOCKED`, clears the schedule, and audits once.
- Rejected alternative: mark the request `approved` until the period ends — the
  database trigger only allows `approved → executed`, and a half-applied state
  would confuse the dashboard.
- The shadow price names its reference model (`config/pricing.yaml` →
  `reference.model`), surfaced as `shadow_price_model` in `/api/metrics/summary`.

### 2026-11-12 · HTTP boundary details
- `POST /api/tickets` is customer-facing and stays unauthenticated by design;
  dashboard routes require the shared bearer token and fail closed (503) when it
  is unconfigured. Mutation routes return 202 (`tickets` intake returns 201).
- The request-size limit checks `Content-Length` before buffering and only
  buffers when the header is absent (chunked), for every `POST /api/*`.
- Admin seed/ingest/load routes are mounted only with `ENABLE_ADMIN_ROUTES=true`
  and call the same functions as the Makefile targets.
- Test-coverage note: the initial Phase 4 commit's DECISIONS wording implied
  A2–A4 regression coverage that did not exist; this round adds the HTTP matrix,
  job/idempotency/resume/plan-change suites, and worker-level AT2–AT5.

### 2026-11-12 · CPU-only torch in the Linux image
- `sentence-transformers` (needed for KB retrieval embeddings, NOT for the LLM)
  pulls PyPI's CUDA torch on Linux: the API image was 19 GB and every rebuild
  re-downloaded ~2 GB of `nvidia-*` wheels. Embeddings run on CPU, so the Linux
  resolution now uses `https://download.pytorch.org/whl/cpu` (`torch = 2.15.0+cpu`,
  zero `nvidia-*` packages); macOS keeps the PyPI wheel, which is CPU-only anyway
  (platform marker on the uv source).
- `torch` is declared as a direct dependency so `[tool.uv.sources]` applies to the
  transitive requirement; without it, uv locks the default CUDA build.
- Rejected: switching to `fastembed`/ONNX (changes retrieval code and requires
  re-ingest), and uninstalling `nvidia-*` after sync (breaks `uv sync` reproducibility).
- `api` and `worker` now share one image tag (`nimbus-backend:latest`) instead of
  building two identical multi-GB images from the same Dockerfile.

### 2026-11-12 · Clean-compose walkthrough findings
- The first ticket in a fresh container escalated: `kb_search` exceeded the 8 s
  tool timeout while the worker cold-loaded the embedding model. Fail-closed and
  re-runnable, but a deployment should warm the model at startup or raise
  `TOOL_TIMEOUT_S`; compose now mounts a named `hf_cache` volume so the download
  is one-time. `make ingest-kb` runs on the host and does not warm the container.
- A ticket escalated that way kept `escalated=true` and its stale
  `escalation_reason` after a successful re-run (the update used `COALESCE`).
  `apply_run_result` now writes `escalated`/`escalation_reason` for every run and
  `mark_resolved` clears them; history stays in `audit_log`/`trace_events`.
- Verified over HTTP on the fresh stack: submit (201) → pause at `send_reply` →
  approve (202) → exactly one `outbox` row + `reply_executed` audit, replay with
  the same key is identical; refund proposal (agent tool) → approve (202) →
  `amount_refunded_cents` +500 once, invoice `partially_refunded`, three audit
  rows; replayed decide is a no-op.

### 2026-11-13 · Cold start: bake the embedding model and warm it at boot
- The first-ticket timeout above is fixed at the cause, not by raising
  `TOOL_TIMEOUT_S` (which would hide real slow queries and make the safety
  budget meaningless). The image now bakes `BAAI/bge-small-en-v1.5` into
  `/srv/hf` (`HF_HOME`) at build time, so a clean clone needs no runtime
  download; `warm_encoder()` runs once in the API lifespan and once in the
  worker before it polls, sharing the module-level instance the tools use.
- Measured on a deleted `hf_cache` volume (`docker compose down` + rebuild):
  warm-up 3.0 s (api) / 2.8 s (worker), first ticket `awaiting_approval` with
  `kb_supported=true` and `[KB-006]`, no `timeout` escalation. The compose
  volume is mounted at `/srv/hf`, the same path, so the baked copy seeds the
  volume on first create instead of being shadowed.
- Warm-up failure is logged and non-fatal: retrieval then fails closed at the
  tool timeout and the ticket escalates, rather than the container refusing to
  start (demo availability). `EMBED_MODEL` overrides are not baked; the startup
  warm-up downloads them once, on the same code path.

### 2026-11-13 · Image contents and the live-worker test trap
- The image now copies `scripts/`, `data/` and `eval/`. The dev-only admin routes
  import those modules, so `POST /api/admin/ingest-kb` and `/load-tickets` used to
  500 with `ModuleNotFoundError` in a container even though they worked on the
  host. `.dockerignore` excludes the host `.venv`/caches (1.2 GB of build context)
  and `data/raw`, but keeps the curated `data/` files the loader needs.
- The worker pool sets `application_name=nimbus-worker`; the integration conftest
  aborts `make test-all` when it sees one on the same database. Stopping the worker
  by hand was the only protection and reviewers run `up` before the tests, so the
  failure looked random. Rejected: a separate test database for now (needs seed +
  KB ingest in the fixtures, ~10 s and more moving parts) — the guard aborts loudly
  and `TEST_DATABASE_URL` still allows isolation.
- `make eval` is documented as not-yet-implemented (`eval/run_eval.py` is Phase 5)
  instead of pointing at a missing file, so the limitation is visible rather than
  discovered by a reviewer.





### 2026-11-13 · Two additive fields the frontend could not work without
- Phase 5 asks for "draft with clickable KB citations" and an approval panel that
  shows the decision outcome, but the API exposed neither: `GET /api/kb/articles`
  returned only `id/title/updated` (no body), and ticket detail had no record of
  the message that was actually sent (it lives in `outbox`). Both were read-only
  gaps in the contract, so they were filled additively:
  `GET /api/kb/articles/{id}` (404 when unknown) and `sent_reply`
  (`{body, approved_by, delivered_at}` or null) on `GET /api/tickets/{id}`.
- Rejected: a separate "sent messages" endpoint (another route to keep in sync for
  one nullable object) and returning the whole article in the citations list
  (duplicates text the UI already renders from `draft_reply`).
- Both are additive, so an existing client is unaffected; PLAN §4.9 records them.

### 2026-11-13 · Phase 6: eval runs in its own database, replay is the published artefact
- Eval runs against `nimbus_eval` (same container, separate database) created and migrated by
  `eval/run_eval.py`; the runner refuses any database whose name does not end in `_eval` unless
  `--allow-non-eval-db` is passed. The compose api/worker point at `nimbus`, so they cannot claim
  eval jobs and the demo state is never touched. Rejected: a separate schema (search_path bugs) and
  truncating the demo DB (destroys the demo, easy to confuse with live data).
- `--llm replay` serves a captured real-model run from `eval/replay_cache.json` and **fails on a
  cache miss** instead of calling a provider, so `make eval` reproduces the published table with no
  API key. The capture is refused on a reused database, because responses from earlier runs cannot
  be attributed to the current one. Replay uses provider `groq` internally so cache keys match.
- Tickets are inserted through `app.services.intake.create_ticket` (same identity rule as
  production: sender email only) and driven through `worker.run_one` (jobs table → graph →
  approval gate). The runner never calls a decide endpoint and asserts
  `approved/executed/outbox == 0` before writing results.

### 2026-11-13 · Phase 6: two seed fixtures were not testing what they claimed
- `_force_paid_invoice` refreshed `status`/`amount_refunded_cents` on conflict but not `paid_at`,
  so re-seeding kept the first run's invoice age: the "5 days old, fully refunded" fixture was 90
  days old and failed the window rule instead of the balance rule. The seed's own promise
  ("re-running is idempotent") was false. Now updates `paid_at`, `amount_cents`, `due_at`.
- `refund_outside_window` named an invoice the base seed pays 20 days before `AS_OF` (inside the
  30-day window) and was sent by the duplicate-email customer, so `lookup_customer` failed closed
  and the window rule was never reached. Re-pointed at `INV-0004-M1` (aged to 60 days) from
  `blake.barnes.4`. The manifest wording ("clamp to the remaining 70%") was also wrong: the tool
  rejects amounts above the remaining balance, it never clamps.

### 2026-11-13 · Phase 6: the checkpointer's one-time DDL needed a lock
- `make_checkpointer` runs `PostgresSaver.setup()` per job. Two eval runs at `--concurrency 2`
  raced on `checkpoint_migrations` (unique violation) and the job queue retried the loser.
  `app/worker.py` claims multiple worker copies are safe, so this was a first-boot production risk
  too, not only an eval artefact. A module-level lock plus a per-DSN "already set up" set fixes it
  in-process; cross-process first boot still relies on the job retry, which is what attempts are
  for.

### 2026-11-13 · Phase 6: real-model output shape is not the stub's
- `gpt-oss-120b` omitted the `reason` field in the support check, which was required, so every
  live ticket failed with `INTERNAL_ERROR`. `reason` is cosmetic (trace/dashboard only, never a
  gate), so it now defaults to empty; `support_v3.txt` lists every field explicitly.
- The repair re-ask claimed the reply "was not valid JSON" even when it was valid JSON that failed
  validation, and `parse_structured` threw away the field name. Both now name the schema error, so
  the retry can actually fix it. Prompt change is a new version (`support_v3`), not an edit to v2,
  because the version string is recorded in the trace and the eval manifest.

### 2026-11-13 · Phase 6: the test suite is pinned to the stub provider
- `LLM_PROVIDER=groq` in `.env` is right for demos and eval runs, but `build_graph(pool)` reads it,
  so 18 integration tests started calling the real model and failing on non-stub drafts.
  `tests/conftest.py` pins `settings.llm_provider = "stub"` for the session. Tests must not depend
  on a vendor, a key, or the developer's `.env`; a test that needs a real provider must construct
  it explicitly.

### 2026-11-13 · Phase 6: eval-set composition and metric populations
- 43 rows = 16 hand-written edge tickets (exported from the seed) + 23 dataset rows drawn
  deterministically from `tickets_all.jsonl` + 4 authored rows. Disjoint from `data/dev/` by
  content hash **and** by `(source_dataset, source_row)`; the old test only checked content against
  `handwritten.jsonl`, so the real eval set could have overlapped dev silently.
- The mix is deliberately skewed towards escalation (44% answer vs ~50% target, 21% not-in-KB vs
  ~20%): the expensive error is a confident wrong answer, and the set has to be able to measure
  it. Deviations are reported in the run, not smoothed over.
- Triage accuracy is measured only on rows that reached the triage node (a `BAD_TICKET` never
  does) and the skipped ids are listed. Tool choice counts a proposal that the tool correctly
  rejected as a correct call; the rejection is reported separately as "proposal rejections".

### 2026-11-13 · Phase 6: pace eval runs by tokens, and never publish a failed run
- A 43-ticket run makes ~130 LLM calls. Pacing by request count was not enough: grounding prompts
  carry five KB chunks (~2.5k tokens) while triage prompts are ~700, so the same call rate costs
  very different token counts and the run still hit the provider's per-minute token limit. The
  failures arrived as `INTERNAL_ERROR` on triage/grounding/draft — many victims, one cause.
  `--tpm` now holds a sliding 60 s token budget and serialises calls; `--rpm` stays for
  request-rate limits. Measured: the throttled run sustained ~11.7k tokens/min before failing, so
  the documented run uses `--tpm 6000` (~16 min for 43 tickets).
- A run with any internally failed ticket exits 1 and refuses `--write-replay`: replaying a
  throttled run would faithfully reproduce the failures. `--allow-failures` exists only for
  debugging a single ticket.
- Node trace errors now carry the exception message (`LLMError: LLM JSON did not match ...
  (reason: Field required)`), not just the class name. The customer-facing escalation detail stays
  generic; the trace is the internal diagnostic surface and losing the message made a
  rate-limit burst indistinguishable from a schema bug.

### 2026-11-13 · Phase 6: the headline capture runs on gpt-oss-20b for now
- The configured model is `openai/gpt-oss-120b`, but its free-tier daily budget (200k tokens) was
  consumed while diagnosing F1/F10, and one full run costs ~127k. An experiment on 20b was chosen
  over waiting a day, with the model named in the run's manifest and report. 20b has its own
  200k/day and an 8k TPM ceiling; `--tpm 6000` keeps the run under it.
- Re-capturing on 120b when its budget resets is a one-command swap (`make eval-live`) plus a commit
  of `eval/replay_cache.json`; nothing else changes, because the report reads the model from the
  manifest.
- The replay loader takes the model from the capture and prints a note when `LLM_MODEL` differs.
  The cache is the source of truth for a replay: its keys embed the model, so refusing to replay
  because `.env` changed would make the artefact fragile for no benefit.
- `draft_v4`: the draft prompt now lists `reply` and `citations` explicitly, after `gpt-oss-20b`
  omitted `reply` on 8 of 43 tickets. Same lesson as F1 — a schema the model cannot see is a
  schema it will not follow. Version bumped because the trace records it.
- `--tpm` now estimates the next call at the largest call in the window, not the mean: a 2.2k
  grounding prompt after small triage calls exceeded the 8k TPM limit while the average looked
  safe.

### 2026-11-13 · Phase 6: prompt fixes validated on dev, and a plan-parser fallback
- `support_v4`: the old rule ("requests that need account data are supported only if a chunk
  explains what the customer must do") made the model answer `supported=false` for policy
  questions about a specific charge — 15 of 19 answer rows escalated. v4 states that explaining
  the policy/steps is supported without account data, and that inventing account facts is not.
  Measured on dev (24 rows, `grounding_report`): 11/15 covered supported, 8/9 uncovered refused.
  No eval row was consulted when choosing the wording.
- `triage_v2`: added boundary examples (explain a charge = invoice_question, ask for money =
  refund_request, policy question = invoice_question) and priority examples (high = money at
  risk / blocked access). The category drives the branch, so `E-020` had escalated as
  `not_found` purely because an invoice question was classified as a refund request.
- `parse_target_plan(text, current_plan_id=...)`: when the customer names the current plan and one
  target ("I am on the annual Pro plan, move me to Starter") and exactly one mentioned plan
  differs from the active subscription, that plan is the target. Genuine ambiguity ("from Pro to
  Starter, not Business") still raises; the tool still rejects a no-op change.
- `scripts/grounding_report.py` gained `--limit` and `--pause-seconds`: it fired 24 support calls
  back to back and tripped the 8k tokens/minute ceiling, which is the same trap as the eval run.
- `tests/unit/test_triage.py` asserted the literal filename `triage_v1.txt`, so any version bump
  broke it. It now asserts the file matches `PROMPT_VERSION` — the invariant that matters, since
  the trace records the version string.

### 2026-11-13 · Phase 6: uncovered categories are a code gate, not a prompt request
- The 120b capture drafted a billing reply for a possible account compromise (E-036): triage said
  `security_incident`, but the support check found a neighbouring article (KB-004) and answered
  `supported=true`. `COVERAGE.md` says security incidents and feature requests escalate, so that
  boundary now lives in `grounding.py` as `UNCOVERED_CATEGORIES` (Gate 0), and two safety tests
  prove no draft is produced even when the LLM double says "supported".
- Cost: those categories are now deterministic escalations, so the eval rows for them measure the
  guard rather than the model. Safety beats measurement here; the remaining `not_in_kb` rows
  (third-party API asks, fax) still test the model.
- `triage_v3` removes the sentiment sentence added in v2 ("a polite request is neutral even when
  it asks for money back"): it generalised and took sentiment accuracy from 0.71 to 0.415. The
  category/priority examples that actually helped (0.902→0.976 and 0.585→0.829) stay.
- `--resume` exists because a 30-minute drain timeout threw away 39 completed tickets. It reuses
  tickets whose latest job is done, re-queues interrupted ones through `enqueue_rerun` (new
  checkpointer thread, guarded transition) and refuses `--write-replay` unless the database holds
  exactly the eval tickets and nothing else. That turned a 135k-token re-run into a 10k resume.
- `eval.report` picked the newest `*.json`, which after `make eval-ablation` is the ablation file,
  so `make eval` failed on its own report step. It now skips files that are not run results.
