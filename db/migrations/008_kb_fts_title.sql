-- 008_kb_fts_title.sql — include article title in full-text search.

DROP INDEX IF EXISTS kb_chunks_tsv_idx;
ALTER TABLE kb_chunks DROP COLUMN IF EXISTS tsv;
ALTER TABLE kb_chunks
    ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || text)
    ) STORED;
CREATE INDEX IF NOT EXISTS kb_chunks_tsv_idx ON kb_chunks USING GIN (tsv);
