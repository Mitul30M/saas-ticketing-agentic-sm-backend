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
	uv run python eval/run_eval.py

lint:
	uv run ruff check app tests scripts eval db
	uv run mypy app

fmt:
	uv run ruff format app tests scripts
