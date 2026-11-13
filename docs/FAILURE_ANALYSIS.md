# Failure analysis

Findings from Phase 6. Each entry has the ticket, what was expected, what actually happened
(trace excerpt), the root cause, and whether it was fixed (with the evidence) or left open.
Entries marked **open** need the full `--llm groq` run to confirm their size.

Ticket ids are eval ids; the database ids change every run because the eval database is rebuilt.

---

## F1 — Every real-model ticket failed at the grounding check

**Ticket:** E-008, E-010, E-011 (3 of 3 in the first live slice)
**Expected:** `answer` / `propose_refund` — i.e. a grounded verdict.
**Actual:** `agent_outcome: failed`, `escalation_reason: INTERNAL_ERROR`, for all three.

```
node              kind       status  error
triage            llm        ok
kb_search         tool       ok
grounding_check   llm_retry  error   LLMError
grounding_check   error      error   LLMError
escalate_to_human tool       ok      {"reason_code": "INTERNAL_ERROR"}
```

The raw model output (reproduced directly against the same prompt):

```json
{"supported": true, "conflicting": false,
 "claims": [{"claim": "...", "kb_id": "KB-003"}, ...]}
```

**Root cause:** two bugs stacked. `gpt-oss-120b` omitted `reason`, which was a required
`Field(min_length=1)`; pydantic rejected the whole verdict. The repair re-ask then said "your
previous reply was not valid JSON", which was false and sent the model chasing a syntax problem
it did not have, so the retry failed identically. `parse_structured` also discarded the
validation detail, so the trace only said "did not match the requested schema".
**Fix:** `reason` defaults to `""` (it feeds the trace, never a gate); `support_v3.txt` lists every
field explicitly; the repair message and `LLMError` now name the offending field
(`LLM JSON did not match the requested schema (reason: Field required)`).
**Evidence:** after the fix, E-010 proposes a refund and E-011 drafts a reply; both reach the gate.

## F2 — Concurrent jobs collided on the checkpointer's one-time DDL

**Ticket:** any two tickets at `--concurrency 2` on a fresh database.
**Expected:** both runs complete on attempt 1.
**Actual:** `psycopg.errors.UniqueViolation: duplicate key value violates unique constraint
"pg_type_typname_nsp_index" ... Key (typname, typnamespace)=(checkpoint_migrations, 2200)`; the
job queue retried and the retry succeeded, so it surfaced only as `job_attempts: 2`.

**Root cause:** `make_checkpointer` ran `PostgresSaver.setup()` (DDL + a migration row) on every
job, unsynchronised. `app/worker.py` documents that multiple copies are safe, so this was a real
production risk on first boot, not just an eval artefact.
**Fix:** module-level lock + a per-DSN "already set up" set in `app/agent/checkpointer.py`, with
`tests/unit/test_checkpointer.py` covering the concurrency and the per-database case.
**Evidence:** 6 tickets at `--concurrency 4` → every job `done/1`.

## F3 — The "outside window" fixture never tested the window

**Ticket:** `refund_outside_window` (E-015)
**Expected:** `escalate` / `business_rule` — refusal by the 30-day window.
**Actual:** the ticket escalated at **identity**: its sender `michael.jordan.1` has a duplicated
email (a deliberate edge case), so `lookup_customer` failed closed before any invoice was read.

**Root cause:** the fixture named `INV-0001-M2`, which the base seed pays **20 days** before
`AS_OF` — inside the window — and used the duplicate-email customer as the sender. The manifest
claimed "60-day-old invoice", so nothing caught the drift.
**Fix:** `INV-0004-M1` is aged to 60 days and the ticket moved to `blake.barnes.4`
(`db/seed/edge_cases.py`). `refund_window_days=30`, so the rule now fires.
**Evidence:** `propose_refund` → `business_rule: invoice is outside the 30-day refund window`.

## F4 — Re-seeding did not move `paid_at`, so fixtures drifted

**Ticket:** `refund_ask_on_fully_refunded` (E-014), `partial_refund_remaining` (E-013),
`refund_over_remaining` (E-012)
**Expected:** `INV-0002-M4` = 5 days old (inside the window, fully refunded), `INV-0005-M3` = 90 days
old.
**Actual:** `INV-0002-M4` was 90 days old and `INV-0005-M3` 45 days, so the fully-refunded case
failed the *window* check instead of the *balance* check.

**Root cause:** `_force_paid_invoice` inserted `paid_at` but its `ON CONFLICT DO UPDATE` only
refreshed `status` and `amount_refunded_cents`. The first seed won and every later seed kept the
stale age, so the seed was not idempotent — a violation of the "deterministic seed" rule.
**Fix:** update `paid_at`, `amount_cents`, `due_at` on conflict.
**Evidence:** fresh seed now reports 5 / 10 / 60 / 90 days exactly as the manifest says, and the
tool returns `refunds require a paid invoice` for the fully refunded one.

## F5 — A fully refunded invoice reports `invalid_input`, not "nothing left"

**Ticket:** `refund_ask_on_fully_refunded` (E-014)
**Expected:** the reason a human sees should be "nothing left to refund".
**Actual:** the planner derives `amount = paid - refunded = 0`, `RefundProposalInput.amount_cents`
is a `PositiveInt`, so validation fails first: `invalid_input: amount_cents: Input should be
greater than 0`.

Fail-closed and safe (nothing is proposed), but the reason is misleading for both the operator and
the eval's reason-code analysis.
**Root cause:** the "no stated amount ⇒ remainder" rule in `orchestration.py` can produce 0.
**Status:** open. Proposed fix: when the derived remainder is 0, return `escalation_update(
"business_rule", "nothing left to refund on this invoice")` instead of calling the tool. Cheap,
but it changes an orchestration branch, so it should land with its own test.

## F6 — The promise denylist missed "I have refunded"

**Ticket:** none in the set yet — found by the metric's own unit test.
**Expected:** a draft saying "I have refunded $20.03 to your card" is rejected as a promise.
**Actual:** `rejects_promise_language` returned `False`; the patterns covered "will process",
"has been approved", "I've issued your refund" but not the past-tense "I refunded".

**Root cause:** an unenumerated phrasing. The deny-list is defence in depth (the approval gate is
the control), but a draft that claims money already moved would still mislead a customer.
**Fix:** added `\b(i|we)( have|'ve)?\s+refunded\b` and two regression cases; the
"no KB sentence trips the denylist" false-positive guard still passes.

## F7 — A documented past-due question escalated on the real model (open)

**Ticket:** E-008 "Why is my account flagged?" (past-due notice, paid with Amex 0005)
**Expected:** `answer`, citing KB-002 (due dates/open balances) and/or KB-003 (failed/pending
payments).
**Actual:** `awaiting_human` / `LOW_CONFIDENCE`; no draft.

```
triage            llm  ok  {"category": "invoice_question", "priority": "high", ...}
kb_search         tool ok  [KB-003, KB-002, KB-005, KB-004, KB-007]
grounding_check   llm  ok  {"supported": false, "unsupported_claims": 1}
escalate_to_human tool ok  {"reason_code": "LOW_CONFIDENCE"}
```

**Root cause (hypothesis):** either the best RRF score sits under `grounding_threshold` (the gate
fires before the support check and would trace a `decision` row instead of an `llm` row — the trace
shows the `llm` row, so the support check is the one saying no), or the support check treats
"account data" as unsupported because no chunk explains *this customer's* balance. The chunks
retrieved are the right ones.
**Status:** open — needs the full run to see whether it is one ticket or a systematic refusal of
account-specific questions. Candidate fixes: a prompt clause distinguishing "explain the concept"
from "state this customer's balance", or one more KB article on reading a past-due notice.

## F8 — Hybrid retrieval ranks below vector-only on the eval set (open)

**Ticket:** the retrieval ablation, not one ticket.
**Expected:** hybrid (BM25 + vector + RRF) ≥ either channel alone.

| variant | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|
| bm25 | 0.391 | 0.565 | 0.826 | 0.530 |
| vector | 0.609 | 0.913 | 0.957 | 0.762 |
| hybrid | 0.522 | 0.783 | 0.870 | 0.667 |
| hybrid+rerank (experimental) | 0.565 | 0.826 | 0.913 | 0.703 |

**Root cause (hypothesis):** these are short synthetic tickets whose wording often matches the
article title lexically; RRF gives a BM25 list full of weak matches the same weight as a strong
vector list, which pushes a correct chunk down.
**Status:** open. This is measured on the **eval** split, so it must not be used to change
production retrieval. Next step: reproduce the gap on `data/dev/`, and if it holds, try
weighted RRF (`w_fts` / `w_vec` in config) tuned on dev only.

---

## Method notes

- Every finding above is reproducible: `make eval-stub` for the harness, `make eval-ablation` for
  F8, `tests/unit/test_checkpointer.py` and `tests/unit/test_promise_denylist.py` for F2/F6.
- Failure analysis is generated from the run's own records (`eval/results/<run>.json`), so the
  "false answers" and "over-escalations" lists in the report are the raw material for new entries.
- No test was loosened to make a fix pass; F3/F4 changed fixture data, F1/F2/F6 changed code.
