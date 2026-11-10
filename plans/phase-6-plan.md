# Phase 6 — Evaluation, safety tests, failure analysis (Day 4)

> Source: `PLAN.md` Part 7, Phase 6.

## II. Checklist

- [ ] Build `data/eval/eval_set.jsonl` (≥40) with gold labels per 4.11; **hand-review every label**; commit `eval/LABELING_GUIDE.md` (definitions of category/priority/sentiment, when to escalate).
- [ ] Tune prompts/τ on `data/dev/` only; then **freeze** (tag commit `eval-freeze`).
- [ ] `eval/run_eval.py`: runs the graph on each ticket in an isolated schema/fixture DB with auto-approve **disabled** (it must stop at the gate; evaluate proposals), supports `--llm mock|real`, `--limit`, `--concurrency`; stores per-ticket results + traces.
- [ ] `eval/metrics.py` & `report.py`: all metrics in 4.11 → markdown table + JSON; confusion matrices; latency percentiles; cost per ticket and ×1,000; cache hit-rate.
- [ ] Retrieval ablation script (BM25 / vector / hybrid / hybrid+rerank) → table.
- [ ] Safety tests `tests/safety/`: **AT1** no refund executes without approval (try to bypass via API ordering, direct tool calls, injection text); **AT2** not-in-KB ⇒ escalated; **AT3** injection text ⇒ same tool plan/decision as a clean control ticket, plus a few injection variants; **AT4** forced tool failure/timeout ⇒ handled, logged, ticket not lost; **AT5** adversarial confusing ticket ⇒ halts ≤ step limit.
- [ ] Select ≥5 (aim 8) failures; write `docs/FAILURE_ANALYSIS.md` with ticket, expected/actual, trace excerpt, root cause, fix/next step. Fix cheap ones and re-run to show before/after.

## Definition of Done (verify with commands)

- `make eval` reproduces the results table
- All five safety tests green via `make test`
- `docs/FAILURE_ANALYSIS.md` has ≥5 entries, each linked to a trace

## Notes & key facts

- **Acceptance tests (PLAN §1.4) = AT1–AT5** (see checklist above) — tests-first territory; they must stay green at all times after this phase. Do not "fix" tests by loosening them.
- **Eval discipline:** `data/dev/` (tuning) and `data/eval/` (report) are **disjoint**; τ + prompts frozen on dev only, tag `eval-freeze` before the final run. Never read gold labels inside the agent or the mock LLM.
- **Eval set mix (PLAN §4.11):** ~50% KB-supported straightforward, ~20% not-in-KB, ~10% refund edge cases, ~10% injection/safety, ~10% confusing/ambiguous/tool-failure-injected. Gold labels include `expected_tools`, `expected_outcome`, `must_not`, `tags`.
- **Metrics:** triage accuracy (+ macro-F1, confusion matrices) · tool-choice P/R/F1 (+ forbidden-tool count) · grounded-reply rate (all citations valid ∧ ⊆ retrieved ∧ judged supported; spot-check 10 by hand) · escalation P/R (+ false-answers on not-in-KB) · safety (0 unapproved executions, injection pass rate, step-limit adherence) · cost (tokens, $/ticket, shadow-priced ×1,000, cache hit-rate) · latency p50/p95.
- **Modes:** `--llm mock` = plumbing/CI only; headline numbers from a real model — always state the model name. Report failures honestly.
- Mock mode must not read gold labels.
- Results path: `eval/run_eval.py` → `eval/results/<run>.json` + markdown.

## Log & achievements (append)
