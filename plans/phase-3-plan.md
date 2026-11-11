# Phase 3 — LangGraph agent, grounding, abstention, failure path (Day 2)

> Source: `PLAN.md` Part 7, Phase 3.

## II. Checklist

- [x] `agent/state.py` typed state; `agent/graph.py` per 4.4; `PostgresSaver` checkpointer; `thread_id=ticket_id`.
- [x] Nodes: `intake_validate`, `injection_screen`, `triage`, explicit no-side-effect `plan`/`tool_executor`/`propose_actions`, `grounding_check`, `draft_reply`, `approval_gate` (`interrupt`), `execute_approved`, `escalate`, `failure_handler`, `finalize`.
- [x] Step counter + `recursion_limit`; `STEP_LIMIT` configurable; exceeding → failure path with reason `STEP_LIMIT`.
- [x] Retry policy (tenacity) for retryable tool/LLM errors; each attempt traced.
- [x] Grounding rule per 4.5 (threshold config, support check, citation validator, deterministic recency tiebreak).
- [x] CLI: `python -m app.agent.run --ticket <id>` prints trace summary and final state.
- [x] Generate and commit the graph diagram (`graph.get_graph().draw_mermaid()` → `docs/architecture.md`).

## Definition of Done (verify with commands)

5 sample tickets run end-to-end:
- (a) KB-answerable → draft with citations
- (b) not-in-KB → escalated
- (c) valid refund → `pending_approval` row + graph paused at approval gate
- (d) injection → flagged, no behaviour change
- (e) confusing → stops at step limit and escalates

Plus: process restart mid-pause → run resumes from checkpoint.

The orchestration now resolves sender-owned customer/invoice/subscription
evidence and inserts only `pending_approval` refund or plan-change proposals.
The full five-scenario DoD remains open until the DB-backed action scenario and
approval API resume path are run against PostgreSQL. The graph still never
executes refunds, plan changes, or replies.

## Notes & key facts

- **Graph topology (PLAN §4.4):** intake_validate → injection_screen → triage → plan ⇄ tool_executor → grounding_check → (escalate | draft_reply → propose_actions → approval_gate(interrupt) → execute_approved → finalize). `failure_handler` catches any node error / step limit / retries exhausted → escalate w/ reason → finalize. Every failure ends in a SAFE state (`awaiting_human` + reason), never a crash or silent drop.
- **Grounding & abstention (PLAN §4.5) — the key quality rule.** A reply may be drafted only if ALL hold:
  1. Retrieval confidence ≥ threshold τ (calibrated on **dev split only**, then frozen).
  2. LLM support check maps each claim → supporting `kb_id`; unsupported claims ⇒ abstain.
  3. Every `[KB-xxx]` citation ∈ retrieved set (deterministic check); invented IDs ⇒ reject.
  4. Recency conflict → most recent `updated` wins; still conflicting ⇒ escalate.
  Otherwise escalate with `NOT_IN_KB` / `LOW_CONFIDENCE` / `CONFLICTING_SOURCES`. Never guess.
- **Persistence:** `langgraph-checkpoint-postgres` `PostgresSaver`, same DB; approval resumes via `Command(resume=...)` from an API call.
- **Injection screen FLAGS only** — may tighten behaviour (force human review), never loosen permissions.
- Reason codes (PLAN Part 9): `NOT_IN_KB`, `LOW_CONFIDENCE`, `CONFLICTING_SOURCES`, `OWNERSHIP_MISMATCH`, `CUSTOMER_NOT_FOUND`, `AMBIGUOUS_CUSTOMER`, `POLICY_VIOLATION`, `TOOL_FAILURE`, `STEP_LIMIT`, `INJECTION_SUSPECTED`, `NEEDS_HUMAN_JUDGEMENT`.
- Graph nodes call services/tools, not the DB directly (layering rule).

## Log & achievements (append)

- 2026-11-11: Phase 3 graph, grounding gates, approval interrupt, checkpoint
  resume, retry handling, CLI runner, and integration scenarios are implemented.
  Fixed the resume test to use `Command(resume=...)`, corrected integration fixture
  cleanup, generated `docs/architecture.md`, and verified `make test-all` with
  121 passing tests. Phase remains open until the human executor/service boundary
  and all five DoD scenarios are reviewed against the final approval API.
- 2026-11-11: Added `services/approvals.py` and `services/executor.py`. The graph
  now records an approved reply as `action_proposed` only; it never writes the
  outbox. The service layer records one final approval, audits it, locks and
  re-verifies it, then performs one replay-safe outbox insert. Full suite remains
  green: 121 passed.
- 2026-11-11: Added explicit no-side-effect orchestration/failure stages and
  deterministic newest-source conflict handling. Phase 3 validation now passes
  with 123 tests.
- 2026-11-11: Wired deterministic refund and plan-change intent planning,
  sender-owned evidence lookups, proposal tools, and action details into the
  approval interrupt. Added paid_at refund-window enforcement, non-empty
  grounding/citation gates, immediate escalation on action lookup failure, and
  registry trace-failure containment. Unit/lint validation passes; DB-backed
  action verification remains pending.
