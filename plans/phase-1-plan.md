# Phase 1 — Database, seed data, KB (Day 1)

> Source: `PLAN.md` Part 7, Phase 1.

## II. Checklist

- [ ] `db/migrations/001_extensions.sql` (`vector`, `pgcrypto`/`uuid-ossp`).
- [ ] `002_core.sql`: customers, plans, subscriptions, invoices, payments (constraints per 4.2.2).
- [ ] `003_support.sql`: tickets, refund_requests, plan_change_requests, approvals, audit_log.
- [ ] `004_platform.sql`: trace_events, idempotency_keys, llm_cache, jobs (if used).
- [ ] `005_kb.sql`: kb_articles, kb_chunks + GIN + HNSW indexes.
- [ ] DB triggers/constraints: refund state-machine guard; `amount_refunded_cents <= amount_cents`; audit_log append-only (revoke UPDATE/DELETE or trigger).
- [ ] `db/seed/seed.py`: deterministic Faker seed (fixed seed value) → ~100 customers, 4 plans, subs, invoices, payments.
- [ ] `db/seed/edge_cases.py`: all edge cases listed in 4.2.2; write `data/seed_manifest.json` mapping edge-case name → entity IDs.
- [ ] Write **25–40 KB articles** (`data/kb/KB-001…`), ~70% coverage design, 2–4 near-duplicates; record in `data/kb/COVERAGE.md` which ticket topics are covered/uncovered (this is ground truth for eval).
- [ ] `scripts/ingest_kb.py`: parse front-matter, chunk (heading-aware), embed (cached), upsert; idempotent (hash-based skip).
- [ ] Download Tobi-Bueck CSV into `data/raw/` (gitignored). Read dataset card; record provenance note in `docs/DECISIONS.md`.
- [ ] `scripts/link_tickets.py`: filter EN → pick 6–8 topics → clean placeholders → rewrite to Nimbus context → assign customers/invoices → write `data/tickets_all.jsonl` (≥80) + hold out dev split.

## Definition of Done (verify with commands)

- `make seed` twice → identical row counts
- `make ingest-kb` twice → no duplicate chunks
- ≥80 linked tickets with valid `customer_id`s
- `data/seed_manifest.json` lists every edge case

## Notes & key facts

- **Schema source of truth:** PLAN §4.2.2 — refund state machine `pending_approval → approved → executed` (or `rejected`/`failed`) enforced **in code AND by DB trigger**. Lines: agent's role only gets INSERT on `refund_requests` with `status='pending_approval'` (CHECK + restricted role/RLS if time allows).
- **Audit_log append-only** — enforce via trigger or revoke UPDATE/DELETE; don't weaken to pass tests.
- **Seeded edge cases (deliberate, labelled):** unknown customer · duplicate email · customer with no payments · invoice fully refunded · partially refunded · outside refund window (>30 days) · amount > paid − refunded · failed payment · past_due subscription · suspended account · annual-plan customer (proration) · invoice belonging to a *different* customer.
- **Seed size:** ~100 customers, 4 plans, ~150 subs, ~400 invoices/payments, Faker fixed seed → deterministic.
- **KB:** 25–40 md articles, front-matter (`id`, `title`, `category`, `updated`); ~30% topics deliberately uncovered → `kb_supported=false` ground truth. 2–4 near-duplicate/conflicting-by-date articles (e.g. Refund policy 2024 vs 2025) so recency tiebreak matters.
- **Chunking:** heading-aware, ~200–350 tokens, 15% overlap; chunks keep `kb_id`, `title`, `heading_path`. `kb_chunks` with GIN(tsv) + HNSW embedding index; `vector(384)`.
- **Dataset:** Tobi-Bueck/customer-support-tickets, **CC BY-NC 4.0** (attribution needed); do **not** use its `answer` column as ground truth (tone/style only); read the dataset card, record provenance in `docs/DECISIONS.md` + README wording ("derived, rewritten into fictional product; no real customer data").
- AI-generated KB drafts OK **if reviewed and disclosed** in `docs/AI_USAGE.md`.
- Health/safety invariants to respect here: constraints are part of the safety design — never drop them to make a test pass.

## Log & achievements (append)
