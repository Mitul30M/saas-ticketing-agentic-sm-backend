# Report package — Nimbus agentic support triage

This folder contains the two written assessment deliverables, kept separate from the operational
docs:

| File | Contents |
|---|---|
| [`evaluation.md`](evaluation.md) | Labelled evaluation set, evaluation script, published results table, and eight analysed failures with trace excerpts |
| [`design-note.md`](design-note.md) | Two-page note: design choices, cost per 1,000 tickets, limits and next steps |

## Where the evidence lives

| Claim | Evidence |
|---|---|
| 43-row labelled set, 100 % reviewed | [`../data/eval/eval_set.jsonl`](../data/eval/eval_set.jsonl), [`../eval/LABELING_GUIDE.md`](../eval/LABELING_GUIDE.md) |
| Evaluation script | [`../eval/run_eval.py`](../eval/run_eval.py), [`../eval/metrics.py`](../eval/metrics.py), [`../Makefile`](../Makefile) |
| Results table | [`evaluation.md`](evaluation.md) §3 and [`../docs/EVAL_RESULTS.md`](../docs/EVAL_RESULTS.md) |
| Failures with traces | [`evaluation.md`](evaluation.md) §4 and the complete F1–F22 log in [`../docs/FAILURE_ANALYSIS.md`](../docs/FAILURE_ANALYSIS.md) |
| Architecture graph | [`../README.md`](../README.md) §Architecture and [`../docs/architecture.md`](../docs/architecture.md) |
| Setup / Docker / demo | [`../README.md`](../README.md) §Quick start and [`../docs/DEMO_DATA.md`](../docs/DEMO_DATA.md) |

## Reading order

1. `README.md` — what it is, the graph, and how to run the full stack.
2. `design-note.md` — why the system is built this way and what it costs.
3. `evaluation.md` — how it was measured and what failed.
4. `docs/FAILURE_ANALYSIS.md` — the full, unabridged failure log.

The 3–5 minute walkthrough video is submitted externally; the repo contains the written step-by-step
path and the demo catalogue needed to reproduce every flow.
