.PHONY: up down migrate seed ingest-kb load-tickets test eval lint fmt app-local

# docker compose up -d (db + api)
up:
	docker compose up -d --build
	@echo "Waiting for db health..."; until docker compose ps db | grep -q "healthy"; do sleep 2; done

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
	uv run python -m eval.run_eval --llm groq --write-replay
	uv run python -m eval.report

eval-stub:
	@echo "eval-stub: plumbing only (deterministic stub LLM), not model quality"
	uv run python -m eval.run_eval --llm stub
	uv run python -m eval.report

eval-ablation:
	uv run python -m eval.retrieval_ablation

grounding-report:
	uv run python -m scripts.grounding_report

lint:
	uv run ruff check app tests scripts eval db
	uv run mypy app

fmt:
	uv run ruff format app tests scripts
