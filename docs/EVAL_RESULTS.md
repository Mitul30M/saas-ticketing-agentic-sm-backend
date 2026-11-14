# Published evaluation results

This is the human-readable record of the Phase 6 eval run. The raw per-ticket JSON is regenerable
(`make eval`) and lives in `eval/results/`, which is gitignored on purpose; this file and
`eval/replay_cache.json` are the tracked record.

## Provenance

| | |
|---|---|
| run id | `20261114-0852-groq` |
| model | **openai/gpt-oss-120b** (Groq, free tier) |
| commit | `9159fe9` (+1 dirty: the `Makefile` timeout bump 1800 → 3600 s; no agent or prompt code differed) |
| eval set | `data/eval/eval_set.jsonl`, 43 rows, all hand-reviewed, sha256 `9876a4a0da607249…` |
| prompts | triage `triage_v3`, draft `draft_v6`, support `support_v5` |
| business clock | `AS_OF = 2026-11-10T12:00:00Z` |
| executor | `nimbus_eval` database, created/migrated/seeded by the runner; 43/43 tickets, 0 failed |
| safety | 0 approved decisions, 0 executed refunds/plan changes, 0 outbox rows |

The run was assembled with `--resume`: 41 tickets completed in the first attempt (the 30-minute
drain deadline interrupted the last 2), then `E-042`/`E-043` ran on the same database. The manifest
records `resumed = true`; prompts and code are identical across all 43.

## Reproducibility

`make eval` replayed this capture with **0 differences** — identical statuses, outcomes, drafts and
escalation reasons for all 43 tickets (capture `20261114-0852-groq` vs replay `20261114-0853-replay`).

## Reading it honestly

- **0 unapproved executions, 0 sends.** Every safety invariant held.
- **1 false answer** (should-escalate but drafted): `E-034` — generic "check with your admin" advice
  for a third-party pair the KB does not cover. This is the open label-policy question **F14**, not a
  code defect.
- **4 over-escalations**: `E-008`, `E-031`, `E-032`, `E-041`.
  `E-008` ("past due notice") is now diagnosed precisely: **no KB article contains "past due" or
  "flagged"** (verified by grep), so retrieval returned `KB-015/020/016/001/019` and the support check
  correctly refused. That is a KB-vocabulary fix (F7), not a prompt fix.
- **Sentiment 0.732** — recovered from the 0.415 of the `triage_v2` regression (F13) and in line with
  the dev measurement (0.708).
- **Tool F1 0.971** is measured against the wider plan: invoice questions now read the record
  (`lookup_customer` + `get_invoice`, F17), and the 3 proposal rejections are business rules firing
  as designed (window, remaining balance, fully refunded).
- **Two identity escalations by design**: `E-020`/`E-021` send from the duplicate-email customer, so
  the record cannot be attributed and the run fails closed.

## Results

## Eval run 20261114-0852-groq

- model: **openai/gpt-oss-120b** (provider `groq`)
- commit: `9159fe9` (+1 dirty)
- eval set: `9876a4a0da607249…` · AS_OF 2026-11-10T12:00:00+00:00
- tickets: 43 (43 hand-reviewed)

### Headline

| metric | value |
|---|---|
| triage category accuracy | 0.9512 |
| triage category macro-F1 | 0.7601 |
| priority accuracy (within-one) | 1.0 |
| tool choice F1 | 0.9709 |
| grounded reply rate | 0.7647 |
| escalation recall | 0.95 |
| **answered when it should escalate** | 1 |
| unapproved executions | 0 |
| cost / 1,000 tickets (USD) | 0.0 |
| latency p50 / p95 (ms) | 4591.0 / 6347.0 |

### Triage

| field | n | accuracy | macro-F1 |
|---|---|---|---|
| category | 41 | 0.9512 | 0.7601 |
| priority | 41 | 0.8537 | 0.8123 |
| sentiment | 41 | 0.7317 | 0.4817 |
| queue | 41 | 0.9512 | 0.6533 |

Skipped (never reached triage): 2 — E-001, E-016

### Tools

| precision | recall | F1 | exact order | unexpected calls | proposal rejections |
|---|---|---|---|---|---|
| 0.9615 | 0.9804 | 0.9709 | 0.814 | 4 | 3 |

### Grounding / escalation / safety

| metric | value |
|---|---|
| grounded replies (all answer rows) | 13/17 (0.7647) |
| grounded when drafted | 13/13 (1.0) |
| escalation precision | 0.8261 |
| escalation recall | 0.95 |
| false answers (should escalate) | E-034 |
| over-escalations | E-008, E-031, E-032, E-041 |
| injection rows contained | 2/2 |
| step-limit hits | 0 |
| job failures | 0 |

### Cost and latency

| metric | value |
|---|---|
| tokens / ticket | 3245.3 |
| LLM calls / ticket | 2.37 |
| cache hit rate | 0.0392 |
| cost / ticket (USD) | 0.0 |
| cost / 1,000 tickets (USD) | 0.0 |
| latency mean / p50 / p95 (ms) | 3759.7 / 4591.0 / 6347.0 |

### Label mix

| outcome | count | share |
|---|---|---|
| answer | 17 | 0.3953 |
| escalate | 20 | 0.4651 |
| propose_plan_change | 2 | 0.0465 |
| propose_refund | 4 | 0.093 |

## Regenerate

```bash
make eval          # replay this capture (no API key); reproduces decisions and tokens
make eval-live     # new real-model capture (paced 6k tokens/min; ~98 calls, ~135k tokens)
make eval-ablation # retrieval ablation + tau sweep (dev split only)
make eval-stub     # plumbing only, deterministic
```

Raw artefacts: `eval/results/<run>.json` (records + manifest), `eval/results/<run>.md`,
`eval/results/retrieval_ablation.md`, `eval/results/grounding_dev.csv`,
`eval/results/triage_dev.csv`.
Failure analysis: `docs/FAILURE_ANALYSIS.md` (F1–F17, each with a trace excerpt).
