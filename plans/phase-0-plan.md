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

## Log & achievements (append)

- 2026-11-10 — Repo initialized (`git init -b main`); PLAN.md + AGENTS.md updated for backend-only layout / Python 3.14; directory skeleton per PLAN §4.12 (backend-only), `.gitignore`, and `plans/phase-{0..8}-plan.md` created. Phase 0 infrastructure (pyproject, compose, Makefile, .env.example) **pending** — this is the remainder of Phase 0.
