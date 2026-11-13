.PHONY: up down migrate seed ingest-kb load-tickets test eval lint fmt app-local dev-triage

# docker compose up -d (db + api)
up:
	docker compose up -d --build
	@echo "Waiting for db health..."; until docker compose ps db | grep -q "healthy"; do sleep 2; done

# Full stack including the Next.js console (sibling repo as the build context).
# Kept separate so `make up` still works for a backend-only clone.
up-web:
	docker compose --profile web up -d --build
	@echo "Console: http://localhost:3000   API: http://localhost:8000"
	@echo "First run? make migrate seed ingest-kb (then reload the console)."

down:
	docker compose down

app-local:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Apply db/migrations/*.sql in order (idempotent via schema_migrations)
migrate:
	uv run python -m app.migrate

seed:
	uv run python -m db.seed.seed

ingest-kb:
	uv run python -m scripts.ingest_kb

load-tickets:
	uv run python -m scripts.load_tickets

test:
	uv run pytest tests/unit -q

test-all:
	uv run pytest tests/unit tests/integration tests/safety -q

eval:
	@echo "eval: replaying the captured real-model run (no API key needed)"
	uv run python -m eval.run_eval --llm replay
	uv run python -m eval.report

eval-live:
	@echo "eval-live: fresh real-model run; rewrites eval/replay_cache.json"
	@echo "  paced at 6000 tokens/min: free tiers limit at 8000/min and 200k/day"
	uv run python -m eval.run_eval --llm groq --concurrency 3 --tpm 6000 --write-replay
	uv run python -m eval.report

eval-stub:
	@echo "eval-stub: plumbing only (deterministic stub LLM), not model quality"
	uv run python -m eval.run_eval --llm stub
	uv run python -m eval.report

eval-ablation:
	uv run python -m eval.retrieval_ablation

grounding-report:
	uv run python -m scripts.grounding_report

dev-triage:
	uv run python -m scripts.dev_triage_report --pause-seconds 6

lint:
	uv run ruff check app tests scripts eval db
	uv run mypy app

fmt:
	uv run ruff format app tests scripts
