# Published evaluation results

This is the human-readable record of the Phase 6 eval run. The raw per-ticket JSON is regenerable
(`make eval`) and lives in `eval/results/`, which is gitignored on purpose; this file and
`eval/replay_cache.json` are the tracked record.

## Provenance

| | |
|---|---|
| run id | `20261113-1018-groq` |
| model | **openai/gpt-oss-120b** (Groq, free tier) |
| commit | `04e020a` (the run predates the F12/F13 fixes below) |
| eval set | `data/eval/eval_set.jsonl`, 43 rows, all hand-reviewed, sha256 `7ee7ebe2daa1493e…` |
| prompts | triage `triage_v2`, draft `draft_v4`, support `support_v4` |
| business clock | `AS_OF = 2026-11-10T12:00:00Z` |
| executor | `nimbus_eval` database, created/migrated/seeded by the runner; 43/43 tickets, 0 failed |
| safety | 0 approved decisions, 0 executed refunds/plan changes, 0 outbox rows |

The run was assembled with `--resume`: 40 tickets completed in the first attempt (a 30-minute drain
timeout interrupted 3), then the remaining 3 ran on the same database. `manifest.resumed = true`
records this, and the prompts/code are identical across all 43.

## Reading it honestly

- **0 unapproved executions, 0 sends, 0 announcements.** The safety invariants held on every ticket.
- **Two false answers** (a should-escalate ticket that produced a draft): `E-036` (a possible account
  compromise drafted as a billing reply, fixed in code after this run — F12) and `E-034` (generic
  integration advice for a not-in-KB product pair, an open label-policy question — F14).
- **Sentiment 0.415 is a self-inflicted regression** from a clause added in `triage_v2` (F13);
  `triage_v3` removes it but this table predates that, so do not quote sentiment as model quality.
- The tree has since moved past this capture (`triage_v3` + the uncovered-category gate), so
  `make eval` on `main` now stops with *"captured with different prompts"* rather than replaying it.
  Reproduce **this** table at commit `04e020a`; publish a new one with `make eval-live`.

## Results

## Eval run 20261113-1018-groq

- model: **openai/gpt-oss-120b** (provider `groq`)
- commit: `04e020a` (+12 dirty)
- eval set: `7ee7ebe2daa1493e…` · AS_OF 2026-11-10T12:00:00+00:00
- tickets: 43 (43 hand-reviewed)

### Headline

| metric | value |
|---|---|
| triage category accuracy | 0.9756 |
| triage category macro-F1 | 0.7778 |
| priority accuracy (within-one) | 1.0 |
| tool choice F1 | 0.9613 |
| grounded reply rate | 0.7895 |
| escalation recall | 0.8889 |
| **answered when it should escalate** | 2 |
| unapproved executions | 0 |
| cost / 1,000 tickets (USD) | 0.0 |
| latency p50 / p95 (ms) | 4742.0 / 6537.0 |

### Triage

| field | n | accuracy | macro-F1 |
|---|---|---|---|
| category | 41 | 0.9756 | 0.7778 |
| priority | 41 | 0.8293 | 0.7877 |
| sentiment | 41 | 0.4146 | 0.3971 |
| queue | 41 | 0.9756 | 0.6667 |

Skipped (never reached triage): 2 — E-001, E-016

### Tools

| precision | recall | F1 | exact order | unexpected calls | proposal rejections |
|---|---|---|---|---|---|
| 0.956 | 0.9667 | 0.9613 | 0.7907 | 4 | 3 |

### Grounding / escalation / safety

| metric | value |
|---|---|
| grounded replies (all answer rows) | 15/19 (0.7895) |
| grounded when drafted | 15/15 (1.0) |
| escalation precision | 0.8 |
| escalation recall | 0.8889 |
| false answers (should escalate) | E-034, E-036 |
| over-escalations | E-008, E-031, E-032, E-041 |
| injection rows contained | 2/2 |
| step-limit hits | 0 |
| job failures | 0 |

### Cost and latency

| metric | value |
|---|---|
| tokens / ticket | 3291.2 |
| LLM calls / ticket | 2.51 |
| cache hit rate | 0.0278 |
| cost / ticket (USD) | 0.0 |
| cost / 1,000 tickets (USD) | 0.0 |
| latency mean / p50 / p95 (ms) | 4111.9 / 4742.0 / 6537.0 |

### Label mix

| outcome | count | share |
|---|---|---|
| answer | 19 | 0.4419 |
| escalate | 18 | 0.4186 |
| propose_plan_change | 2 | 0.0465 |
| propose_refund | 4 | 0.093 |

## Regenerate

```bash
make eval          # replay this capture (no API key) — reproduces decisions and tokens
make eval-live     # new real-model capture (paced; ~127k tokens, one per day on the free tier)
make eval-ablation # retrieval ablation + tau sweep (dev split only)
make eval-stub     # plumbing only, deterministic
```

Raw artefacts: `eval/results/<run>.json` (records + manifest), `eval/results/<run>.md`,
`eval/results/retrieval_ablation.md`, `eval/results/grounding_dev.csv`.
Failure analysis: `docs/FAILURE_ANALYSIS.md` (F1–F14, each with a trace excerpt).
