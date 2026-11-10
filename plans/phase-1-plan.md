# Phase 1 — Database, seed data, KB (Day 1)

> Source: `PLAN.md` Part 7, Phase 1.

## II. Checklist

- [x] `db/migrations/001_extensions.sql` (`vector`, `pgcrypto`/`uuid-ossp`). ✅ applied
- [x] `002_core.sql`: customers, plans, subscriptions, invoices, payments (constraints per 4.2.2). ✅ applied
- [x] `003_support.sql`: tickets, refund_requests, plan_change_requests, approvals, audit_log (+ `outbox`). ✅ applied
- [x] `004_platform.sql`: trace_events, idempotency_keys, llm_cache, jobs (if used). ✅ applied
- [x] `005_kb.sql`: kb_articles, kb_chunks + GIN + HNSW indexes. ✅ applied
- [x] DB triggers/constraints: refund state-machine guard; `amount_refunded_cents <= amount_cents`; audit_log append-only (revoke UPDATE/DELETE or trigger). ✅ enforced by triggers; exercised in `tests/integration/test_db_schema.py` (8/8 green)
- [x] `db/seed/seed.py`: deterministic Faker seed (fixed seed value) → ~100 customers, 4 plans, subs, invoices, payments. ✅ `make seed` twice produced stable counts (4 plans, 101 customers, 144 subscriptions, 368 invoices, 348 payments).
- [x] `db/seed/edge_cases.py`: all edge cases listed in 4.2.2; write `data/seed_manifest.json` mapping edge-case name → entity IDs. ✅ 22 manifest entries; repeat run is stable.
- [ ] Write **25–40 KB articles** (`data/kb/KB-001…`), ~70% coverage design, 2–4 near-duplicates; record in `data/kb/COVERAGE.md` which ticket topics are covered/uncovered (this is ground truth for eval).
- [ ] `scripts/ingest_kb.py`: parse front-matter, chunk (heading-aware), embed (cached), upsert; idempotent (hash-based skip).
- [x] Download Tobi-Bueck CSV into `data/raw/` (gitignored). Read dataset card; record provenance note in `docs/DECISIONS.md`. ✅ CSV + dataset card present locally; tickets-only provenance and rewrite policy documented.
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
- **Dataset reality check (owner-confirmed):** Tobi-Bueck is tickets-only — no customers, payments, orders, or KB. Implications for the seed/link work in this phase:
  - Seed **business DB first**, then link tickets to it. `payments` must expose amount, method, status, paid_at, refunded-to-date (schema already has all of these) so refund tools can enforce amount ≤ paid − refunded, the window, and duplicate detection.
  - "Order status" maps to subscription + invoice/billing status tools (`get_subscription`, `get_invoice`) — nothing extra to model.
  - Linking: filter EN → 6–8 topics → assign customer per ticket → inject `invoice_id` + amount into billing/refund ticket bodies → tools can answer them. `answer` column is style reference only, never ground truth.
  - The seed manifest must map which seeded customers/invoices each linked ticket points at.
- **`/Volumes/Mitul/Projects/QuickDrop-Rider-Payment-HelpDesk-Agent` is READ-ONLY owner reference** (patterns for hints/LLM wiring/idempotency/traces/tests); never modify it.

## Log & achievements (append)

- 2026-11-11 — Phase 1 seed verification: `make seed` completed twice with stable row counts; edge-case seeding produced 22 manifest entries. The Tobi-Bueck CSV and dataset card are present under `data/raw/`, and provenance/rewrite constraints are recorded in `docs/DECISIONS.md`.
- 2026-11-11 — Fixed linker formatting and pool-selection bugs; `uv run python -m scripts.link_tickets` now produces 70 load tickets + 10 dev tickets (80 total), all with seeded customer references. The ≥80 load-ticket checklist item remains open; KB authoring and ingestion remain open.
