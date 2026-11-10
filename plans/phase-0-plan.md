# Phase 0 — Bootstrap & repo hygiene

> Source: `PLAN.md` Part 7, Phase 0 (≈1–2 h). One phase at a time; tick PLAN checkboxes only after verifying the DoD with a real command.

## II. Checklist

- [ ] Init git repo; add `.gitignore` (`.env`, `data/raw/`, `node_modules`, `.venv`, `__pycache__`, `.next`).
- [ ] Create directory layout from 4.12 (backend-only; empty packages with `__init__.py`).
- [ ] Backend: `uv init` / `pyproject.toml` with deps: `fastapi uvicorn[standard] pydantic pydantic-settings psycopg[binary,pool] langgraph langgraph-checkpoint-postgres langchain-core tenacity httpx pyyaml pytest pytest-asyncio faker sentence-transformers` (pin versions after first successful install; keep a lockfile). **Python 3.14** (`requires-python = ">=3.14"`).
- [ ] `docker-compose.yml`: services `db` (`pgvector/pgvector:pg16`, volume, healthcheck), `api`; **no `web` service** (frontend is a separate repo); optional `ollama` behind a profile.
- [ ] `.env.example` with `DATABASE_URL`, `LLM_PROVIDER`, `LLM_MODEL`, `EMBED_MODEL`, `AGENT_TOKEN`, `STEP_LIMIT`, `TOOL_TIMEOUT_S`, `REFUND_WINDOW_DAYS`.
- [ ] `Makefile` targets: `up`, `down`, `migrate`, `seed`, `ingest-kb`, `load-tickets`, `test`, `eval`, `lint`.

## Definition of Done (verify with commands)

- `docker compose up db` healthy (healthcheck passes)
- `make migrate` applies an empty migration
- `GET /healthz` returns 200

## Notes & key facts

- **Deviations from PLAN.md (recorded, PLAN updated):**
  - Repo is **backend-only** → `app/` at root (no `backend/` wrapper), no `web/` folder, no `web` compose service; Next.js frontend is a separate repo.
  - Python **3.14** (preinstalled via Homebrew, managed with `uv`), not 3.12. Note in `docs/DECISIONS.md` when created.
- Commits follow the `phase-0: <action> ...` convention from the owner.
- `.gitignore` covers `.env`, `data/raw/`, caches; `uv.lock` **is** committed (do not ignore).
- `data/raw/` is gitignored → recreate locally when downloading the Tobi-Bueck CSV (Phase 1).
- Empty dirs tracked via `.gitkeep`.
- No hard-coded thresholds: STEP_LIMIT, TOOL_TIMEOUT_S, REFUND_WINDOW_DAYS all come from env/config from day one.

## Definition of Done (verify with commands) — ✅ verified 2026-11-10

- [x] `docker compose up db` healthy — pgvector/pg16 @ host 5433
- [x] `make migrate` applies migrations — 18 tables; second run → "migrations up to date"
- [x] `GET /healthz` returns 200 (`{"status":"ok"}`)
- [x] `make test` 3 unit tests green; `pytest tests/integration` 8/8 green (real DDL + trigger behavior vs running Postgres)
- [x] `make lint` — ruff + mypy clean on `app/`

## Log & achievements (append)

- 2026-11-10 — Repo initialized (`git init -b main`); PLAN.md + AGENTS.md updated for backend-only layout / Python 3.14; directory skeleton per PLAN §4.12 (backend-only), `.gitignore`, and `plans/phase-{0..8}-plan.md` created. Phase 0 infrastructure (pyproject, compose, Makefile, .env.example) **pending** — this is the remainder of Phase 0.
- 2026-11-10 (Phase 0 build) — `pyproject.toml` (py≥3.14, all PLAN deps + `langchain-groq`, `mypy`); `uv sync` OK, `uv.lock` committed. `docker-compose.yml` (pgvector/pg16, 5433:5432, healthchecks, api build), `Dockerfile` (3.14-slim + uv frozen), `.env.example`, `Makefile` (all 9 PLAN targets). `app/config.py` (pydantic-settings incl. STEP_LIMIT/TOOL_TIMEOUT_S/REFUND_WINDOW_DAYS), `app/db.py` (connect + file-based `run_migrations` w/ `schema_migrations` + `trace()` helper, reference pattern), `app/migrate.py` CLI, `app/main.py` (`/healthz`, CORS). Migrations 001–005 written: extensions → core (payments refund≤paid CHECK) → support (refund state-machine trigger, one-open-refund-per-invoice partial unique, audit_log append-only trigger, outbox) → platform (trace_events, idempotency_keys, llm_cache, jobs) → KB (tsv GIN + HNSW vector(384)). Tests: 3 unit green (`make test`), 8 integration skip-if-down incl. **trigger behavior tests**. `ruff` + `mypy` clean.
- Verification commands to run once Docker is on: `make up`, `make migrate`, `uv run pytest tests/integration -q`, `uv run uvicorn app.main:app` → `GET /healthz` 200, `curl :5433` psql `\dt`.
- LLM: Groq via ChatGroq as in reference (client pattern in `app/llm/` — Phase 2); `LLM_PROVIDER=stub` until key.
- 🔄 **Waiting on owner: `docker compose up -d db` to finish DoD verification.**
- 2026-11-10 (verification) — Docker db container healthy; fixed two issues found live: (1) migration runner split DDL on `;` which broke comment text containing semicolons → now executes each file in one statement pass with full-line `--` comments stripped; (2) Makefile `migrate` target pointed at wrong module (`app.db.migrate` → `app.migrate`), and `test` target now runs `tests/unit` only (`test-all` covers unit+integration+safety). DoD verified fully (see checklist above). **Phase 0 complete; Phase 1 DDL (001–005) already applied as part of this push, Phase 1 seed/KB/ticket-pipeline work remains.**
