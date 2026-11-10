# Phase 7 — Documentation, demo, submission (Day 5)

> Source: `PLAN.md` Part 7, Phase 7.

## II. Checklist

- [ ] `README.md`: what it is, architecture diagram, quickstart (`cp .env.example .env && docker compose up -d && make migrate seed ingest-kb load-tickets`), how to run tests/eval, env vars, data provenance + CC BY-NC attribution, limitations. *(Mention the separate frontend repo.)*
- [ ] `docs/NOTE.md` (≤2 pages): design choices, **cost per 1,000 tickets** (assumptions: avg tokens/ticket, model price, cache hit-rate, shadow vs actual), limits, next steps.
- [ ] `docs/AI_USAGE.md`: precisely what you wrote vs. what AI assisted (code, KB drafts, ticket rewrites, docs) + models used.
- [ ] `docs/DECISIONS.md` cleaned up (trade-offs; alternatives rejected).
- [ ] Clean-clone test: fresh directory, follow README only, everything works.
- [ ] Record **3–5 min video** following `docs/DEMO_SCRIPT.md`: (1) problem + architecture 30s, (2) customer submits ticket, (3) agent triage + grounded draft + citations, (4) refund proposal → approval gate → approve → executed once, (5) not-in-KB escalation, (6) injection ticket, (7) tool failure/step limit/trace view, (8) eval table + cost/latency + idempotency demo.
- [ ] Optional: deploy demo (Render/Fly/VPS with compose) — only if time remains.
- [ ] Reply to recruiter email with repo link, video link, demo link/Docker instructions, note, eval results — **before the 5-day deadline**.

## Definition of Done (verify with commands)

Every item in PLAN §2.1 ticked:
- [ ] GitHub repo (code, README, setup, architecture diagram of the graph)
- [ ] Reproducible Docker setup + 3–5 min walkthrough video
- [ ] Labelled eval set + eval script + results table + ≥5 analysed failures with traces
- [ ] Short note ≤2 pages
- [ ] AI-usage / ownership disclosure
- [ ] Email reply with all links

## Notes & key facts

- Cost section must state the **reference model + pricing** (`config/pricing.yaml`) and separate actual vs shadow-priced; report cache hit-rate.
- Attribution: dataset CC BY-NC 4.0, "derived from a public dataset, rewritten into a fictional product; no real customer data used."
- Keep `docs/DEMO_SCRIPT.md` written early so recording isn't rushed.
- Honest reporting: every metric reproducible via `make eval`; disclose mock vs real model.

## Log & achievements (append)
