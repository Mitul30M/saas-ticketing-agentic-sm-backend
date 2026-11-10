-- 005_kb.sql — knowledge base articles + chunks with hybrid-retrieval indexes.

CREATE TABLE IF NOT EXISTS kb_articles (
    id        TEXT PRIMARY KEY,               -- 'KB-001'
    title     TEXT NOT NULL,
    category  TEXT,
    updated   DATE NOT NULL,
    body      TEXT NOT NULL,
    content_hash TEXT NOT NULL,               -- ingest idempotency (hash-based skip)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id        TEXT NOT NULL REFERENCES kb_articles(id) ON DELETE CASCADE,
    chunk_idx    INTEGER NOT NULL,
    title        TEXT NOT NULL,
    heading_path TEXT[] NOT NULL DEFAULT '{}',
    text         TEXT NOT NULL,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding    vector(384),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kb_id, chunk_idx)
);

CREATE INDEX IF NOT EXISTS kb_chunks_tsv_idx ON kb_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
    ON kb_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS kb_chunks_kb_idx ON kb_chunks (kb_id, chunk_idx);
