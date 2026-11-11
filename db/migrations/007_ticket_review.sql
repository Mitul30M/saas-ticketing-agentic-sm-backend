-- 007_ticket_review.sql — reviewed flag for the manual data-quality gate.
ALTER TABLE tickets
    ADD COLUMN IF NOT EXISTS reviewed BOOLEAN NOT NULL DEFAULT FALSE;
