# Phase 2 — Tools, LLM abstraction, retrieval, triage (Day 1–2)

> Source: `PLAN.md` Part 7, Phase 2.

## II. Checklist

- [x] `llm/base.py`: `LLMClient.generate(messages, schema|None, tools|None, **params)` returning text/structured + usage; providers: `stub`, `groq`; shared retry/repair for JSON; timeout. ✅ cache wiring + repair-once (strip fences → one re-ask, groq only) in `generate()`
- [x] `llm/cache.py`: sha256 cache wrapper (layer 3 idempotency); logs `cache_hit`. ✅ wired into `generate()` via optional `pool` arg; `cache_hit=True` responses reuse stored tokens
- [x] `tools/schemas.py`: Pydantic models for every tool input/output + `ToolError` codes. ✅ `kb_ids` KB- validator now on both proposal inputs
- [ ] Implement tools per 4.3 with ownership/business-rule validation; each wrapped by `registry.call(name, args, ctx)` → validation → timeout → typed result → trace row. ✅ registry + `lookup_customer`/`get_invoice`/`propose_refund` done; `kb_search`, `get_subscription`, `propose_plan_change`, `escalate_to_human` remain
- [ ] `retrieval/hybrid.py`: FTS + vector queries, RRF fusion (k=60); `retrieval/rerank.py`: optional cross-encoder (feature flag).
- [ ] Unit tests: every tool — valid input, each invalid input class, each business-rule failure (unknown customer, ownership mismatch, over-refund, outside window, duplicate open request), timeout simulation. ✅ registry contract (32 unit) + ownership integration (12); business-rule paths for remaining tools remain
- [ ] `agent/nodes/triage.py`: structured output `{category, priority, sentiment, reasons{category,priority,sentiment}}`; enums fixed in one module; deterministic `category → queue` map.
- [ ] Triage prompt files in `agent/prompts/` (versioned, short, ticket text delimited as untrusted).
- [x] Foundation hardening: pooled DB access, `LLM_TIMEOUT_S=25`, per-call retry semantics, migration 008 title-aware FTS, model-aware KB hashes, stale-article pruning, and title/heading prefixes at ingest. ✅ implementation complete

## Definition of Done (verify with commands)

- `pytest tests/unit` green
- Triage runs on 10 dev tickets with valid structured output
- Retrieval returns sensible top-3 for 5 hand-checked queries

## Notes & key facts

- **Tools (PLAN §4.3):** `kb_search`, `get_customer`, `get_subscription`, `get_invoice`, `propose_refund` (write/propose-only), `propose_plan_change` (write/propose-only), `escalate_to_human` (control). ≥4 required by brief; we ship all 7.
- **Tool rules (non-negotiable):** Pydantic validation first → business-rule validation (ownership; amount ≤ paid − refunded; within window; no duplicate open request) → timeout (~8 s, configurable) → typed result `{ok, data | error{code,message,retryable}}` → trace row. **Never raise into the graph. No raw SQL tool, ever.** Write tools only INSERT `pending_approval` rows.
- `propose_refund` requires `kb_ids` (≥1) as evidence.
- **LLM layer:** only via `LLMClient` abstraction (`mock | ollama | openai_compat`); never call a vendor SDK directly from graph/tool code. Mock mode must not read gold labels.
- **Retrieval:** RRF fusion k=60; LLM JSON retry/repair (small local models are weak at structured output — constrain with enums, keep prompts short).
- Triage: enums (category/priority/sentiment) live in ONE module and are reused by prompts, schemas, eval, and UI. Deterministic category → queue map in code, not prompt.
- Ticket text is untrusted data — always delimited in prompts; system policy never includes it (invariant #5).

## Log & achievements (append)

- 2026-11-11: Added the provider-independent `LLMClient` boundary with `stub` and
  `groq` providers, structured-output validation, deterministic category/queue taxonomy,
  initial validated tool contracts, and SHA-256 cache-key helpers. Full provider retry/
  repair, database cache wrapper, registry, and tool implementations remain open.
- 2026-11-11: Fixed the linker payment join, normalized provider failures to `LLMError`,
  made required-field stub output schema-valid, and added cache-key regression coverage.
- 2026-11-11: Added `scripts/load_tickets.py` under the migration-006 intake
  semantics (transport-sender `from_email`, `source='import'`, idempotent via
  `(from_email, content_hash)` skip check). `make load-tickets` now works end
  to end: 60 inserted on first run, 60 skipped on rerun. Seed tickets now
  stamp `source='seed'` on conflict. Added the `stub_example()` convention
  with fallback self-validation (`LLMError` in CI for unsupported shapes);
  3 new unit tests in `tests/unit/test_llm_stub.py` (26 passed total).
- 2026-11-11: Implemented the tool registry (`ToolRegistry`/`ToolSpec`/
  `ToolFailure`) with validation-first, thread+timeout execution, typed
  results, and exactly ONE trace row per call written by the registry (so an
  abandoned handler after timeout can never trace twice). Wired
  `lookup_customer`, `get_invoice`, `propose_refund` in `build_registry(pool)`;
  ownership resolved from `tickets.from_email`, missing==foreign invoice both
  return `OWNERSHIP_MISMATCH` (anti-probing), refund inserts are
  `pending_approval`-only with server-derived idempotency keys. Contract tests
  supplied by the owner written verbatim: 32 registry unit tests + 12
  ownership integration tests.
- 2026-11-11: Wired the DB-backed LLM cache into `generate()` (optional
  `pool` arg; identical requests answered from `llm_cache` with
  `cache_hit=True` and stored token counts) and added repair-once JSON
  handling for real providers (strip code fences → single bounded re-ask).
  Groq responses now record input/output tokens. Added
  `tests/integration/test_llm_cache.py` (6 tests) and the missing `kb_ids`
  `KB-` validator on `PlanChangeProposalInput`. Suite: 76 passed, ruff clean,
  mypy `app/` clean.
