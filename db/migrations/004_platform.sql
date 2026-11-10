-- 004_platform.sql — observability + idempotency platform (PLAN 4.6 / 4.8).

CREATE TABLE IF NOT EXISTS trace_events (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    ticket_id   TEXT NOT NULL,
    node        TEXT NOT NULL,
    kind        TEXT NOT NULL,                 -- node | tool | llm | approval | error
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INTEGER,
    input       JSONB,
    output      JSONB,
    status      TEXT NOT NULL DEFAULT 'ok',    -- ok | error | retry
    error       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    cost_usd    NUMERIC,
    cache_hit   BOOLEAN
);
CREATE INDEX IF NOT EXISTS trace_events_ticket_idx ON trace_events (ticket_id, id);

-- Layer 1 idempotency: mutating endpoints replay stored responses for a repeated
-- Idempotency-Key; same key + different body hash -> 422 (handled in service code).
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_status INTEGER,
    response_body   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key, endpoint)
);

-- Layer 3 idempotency: identical LLM calls are answered from cache ($0, deterministic).
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,              -- sha256(provider|model|params|prompt)
    response    JSONB NOT NULL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Simple background job queue for agent runs (never block the POST request).
CREATE TABLE IF NOT EXISTS jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id   UUID NOT NULL REFERENCES tickets(id),
    kind        TEXT NOT NULL DEFAULT 'run_agent',
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_pending_idx ON jobs (status, created_at);
