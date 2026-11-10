-- 006_safety_and_dashboard.sql — amendments found in the 2026-11-11 review (PLAN "Review amendments").
-- Forward-only. Applies on top of 001–005. Does not weaken any existing constraint.

-- ---------------------------------------------------------------------------
-- 1. tickets: sender identity, dashboard labels, injection flag, soft dedupe
-- ---------------------------------------------------------------------------
-- from_email = TRANSPORT sender (set by the intake service). Tools must resolve the
-- customer from THIS field, never from an email/ID claimed inside the body.
ALTER TABLE tickets
    ADD COLUMN IF NOT EXISTS from_email TEXT,
    ADD COLUMN IF NOT EXISTS from_name  TEXT,
    ADD COLUMN IF NOT EXISTS public_id  UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS source     TEXT NOT NULL DEFAULT 'portal',
    ADD COLUMN IF NOT EXISTS handled_by TEXT NOT NULL DEFAULT 'unassigned',
    ADD COLUMN IF NOT EXISTS agent_outcome TEXT,
    ADD COLUMN IF NOT EXISTS injection_suspected BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE tickets
    ADD CONSTRAINT tickets_source_chk
        CHECK (source IN ('portal','import','seed','eval')),
    ADD CONSTRAINT tickets_handled_by_chk
        CHECK (handled_by IN ('unassigned','agent','human')),
    ADD CONSTRAINT tickets_agent_outcome_chk
        CHECK (agent_outcome IS NULL OR agent_outcome IN
               ('draft_ready','action_proposed','escalated','failed'));

CREATE UNIQUE INDEX IF NOT EXISTS tickets_public_id_idx ON tickets (public_id);

-- Backfill sender from the linked customer for already-loaded rows.
UPDATE tickets t
   SET from_email = c.email,
       from_name  = c.first_name || ' ' || c.last_name
  FROM customers c
 WHERE t.customer_id = c.id AND t.from_email IS NULL;

-- A global UNIQUE(content_hash) swallows a customer's legitimate identical follow-up
-- and collapses two different customers who paste the same text. Dedupe becomes a
-- SOFT, windowed check in the intake service (same from_email + hash within N minutes).
ALTER TABLE tickets DROP CONSTRAINT IF EXISTS tickets_content_hash_key;
CREATE INDEX IF NOT EXISTS tickets_dedupe_idx ON tickets (from_email, content_hash, received_at);

-- ---------------------------------------------------------------------------
-- 2. Approval gate enforced IN THE DATABASE (acceptance test 1)
-- ---------------------------------------------------------------------------
ALTER TABLE refund_requests
    ADD CONSTRAINT refund_approved_needs_approver
        CHECK (status NOT IN ('approved','executed') OR approved_by IS NOT NULL),
    ADD CONSTRAINT refund_executed_needs_timestamp
        CHECK (status <> 'executed' OR executed_at IS NOT NULL);

ALTER TABLE plan_change_requests
    ADD CONSTRAINT plan_change_approved_needs_approver
        CHECK (status NOT IN ('approved','executed') OR approved_by IS NOT NULL),
    ADD CONSTRAINT plan_change_executed_needs_timestamp
        CHECK (status <> 'executed' OR executed_at IS NOT NULL);

-- One FINAL human decision per action: a double-click / replayed approval cannot
-- create a second approved/rejected row. ('edited' rows may repeat for draft tweaks.)
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_final_decision
    ON approvals (action_type, action_ref)
    WHERE action_ref IS NOT NULL AND decision IN ('approved','rejected');

-- Replace the refund guard: same transitions as 003 PLUS "approved requires a recorded
-- human decision row in approvals" (approvals insert must precede the status flip).
CREATE OR REPLACE FUNCTION refund_state_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.status <> 'pending_approval' THEN
        RAISE EXCEPTION 'refund_requests rows must start as pending_approval';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.status = NEW.status THEN
            RETURN NEW;
        END IF;
        IF NOT (
            (OLD.status = 'pending_approval' AND NEW.status IN ('approved','rejected','failed'))
         OR (OLD.status = 'approved'         AND NEW.status IN ('executed','failed'))
        ) THEN
            RAISE EXCEPTION 'illegal refund status transition % -> %', OLD.status, NEW.status;
        END IF;
        IF NEW.status = 'approved' AND NOT EXISTS (
            SELECT 1 FROM approvals a
             WHERE a.action_type = 'refund' AND a.action_ref = NEW.id
               AND a.decision IN ('approved','edited')
        ) THEN
            RAISE EXCEPTION 'refund % cannot be approved without a recorded human decision', NEW.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- plan_change_requests had NO state guard in 003: pending -> executed was possible.
CREATE OR REPLACE FUNCTION plan_change_state_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.status <> 'pending_approval' THEN
        RAISE EXCEPTION 'plan_change_requests rows must start as pending_approval';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.status = NEW.status THEN
            RETURN NEW;
        END IF;
        IF NOT (
            (OLD.status = 'pending_approval' AND NEW.status IN ('approved','rejected','failed'))
         OR (OLD.status = 'approved'         AND NEW.status IN ('executed','failed'))
        ) THEN
            RAISE EXCEPTION 'illegal plan_change status transition % -> %', OLD.status, NEW.status;
        END IF;
        IF NEW.status = 'approved' AND NOT EXISTS (
            SELECT 1 FROM approvals a
             WHERE a.action_type = 'plan_change' AND a.action_ref = NEW.id
               AND a.decision IN ('approved','edited')
        ) THEN
            RAISE EXCEPTION 'plan change % cannot be approved without a recorded human decision', NEW.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER plan_change_state_guard_trg
    BEFORE INSERT OR UPDATE ON plan_change_requests
    FOR EACH ROW EXECUTE FUNCTION plan_change_state_guard();

-- Outbox: a reply can only be "sent" once per approval (retries become no-ops).
ALTER TABLE outbox
    ADD COLUMN IF NOT EXISTS approval_id UUID UNIQUE REFERENCES approvals(id);

-- ---------------------------------------------------------------------------
-- 3. Platform tables: what the worker, cost report and idempotency need
-- ---------------------------------------------------------------------------
ALTER TABLE llm_cache
    ADD COLUMN IF NOT EXISTS provider    TEXT,
    ADD COLUMN IF NOT EXISTS model       TEXT,
    ADD COLUMN IF NOT EXISTS cost_usd    NUMERIC,
    ADD COLUMN IF NOT EXISTS hit_count   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_hit_at TIMESTAMPTZ;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS run_after    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS locked_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS locked_by    TEXT,
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS dedupe_key   TEXT;
-- one live job per (ticket, kind, dedupe_key): enqueue twice = one job
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe_idx ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL;

ALTER TABLE idempotency_keys
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours';
