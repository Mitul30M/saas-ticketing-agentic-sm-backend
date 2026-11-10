# Phase 8 — Interview prep (after submission)

> Source: `PLAN.md` Part 7, Phase 8. Not code — personal readiness for the interview.

## II. Checklist

- [ ] Be able to explain: why propose-only writes; why PostgresSaver; why hybrid+RRF; how τ was chosen; the three idempotency layers; what the eval can't tell you.
- [ ] Rehearse a **live change**: change refund window 30→14 days and show tests; add a new triage category; tighten τ and show escalation shift.
- [ ] Know your numbers: triage accuracy, escalation precision/recall, p95 latency, cost per 1,000 tickets, top 3 failure modes.

## Notes & key facts

- Owner must be able to explain every design choice live and make a small change on the spot — keep code small, modular, well understood; don't accept code you can't explain.
- Candidate-owned deliverables to defend (PLAN §2.2): schema design & safety, graph topology, approval-gate semantics, grounding/threshold calibration, hand-reviewed eval labels, 3-layer idempotency, cost model.
- `docs/DECISIONS.md` is the revision sheet — read it top to bottom before the interview.

## Log & achievements (append)
