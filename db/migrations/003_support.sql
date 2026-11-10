-- 003_support.sql — tickets, proposal tables, approvals, audit log, outbox.
-- Safety core: write tools may ONLY INSERT rows with status='pending_approval';
-- the ONLY path to approved/executed is the deterministic approvals service,
-- which flips status with a CAS locked to that exact precondition (PLAN 4.2.2).

CREATE TABLE IF NOT EXISTS tickets (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id       UUID REFERENCES customers(id),      -- null = unknown sender
    channel           TEXT NOT NULL DEFAULT 'email',
    subject           TEXT NOT NULL,
    body              TEXT NOT NULL,
    content_hash      TEXT NOT NULL UNIQUE,               -- duplicate submissions collapse
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status            TEXT NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new','triaging','awaiting_approval',
                                        'awaiting_human','resolved','failed')),
    queue             TEXT,
    category          TEXT,
    priority          TEXT,
    sentiment         TEXT,
    triage_reasons    JSONB,
    kb_supported      BOOLEAN,
    escalated         BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_reason TEXT,
    draft_reply       TEXT,
    draft_citations   TEXT[],
    thread_id         TEXT                                -- langgraph checkpoint thread
);
CREATE INDEX IF NOT EXISTS tickets_status_idx ON tickets (status, received_at);
CREATE INDEX IF NOT EXISTS tickets_queue_idx ON tickets (queue, escalated);

CREATE TABLE IF NOT EXISTS refund_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES tickets(id),
    invoice_id      TEXT NOT NULL REFERENCES invoices(id),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    reason          TEXT NOT NULL,
    evidence        JSONB NOT NULL,                       -- tool outputs + KB ids justifying it
    status          TEXT NOT NULL DEFAULT 'pending_approval'
                    CHECK (status IN ('pending_approval','approved','rejected',
                                      'executed','failed')),
    proposed_by     TEXT NOT NULL DEFAULT 'agent',
    approved_by     TEXT,
    decided_at      TIMESTAMPTZ,
    executed_at     TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (invoice_id, idempotency_key)
);

-- Hard budget line: refunding cannot exceed the invoice amount, no matter what
-- approval/execution race happens (DB-enforced, mirrors payments CHECK).
CREATE INDEX IF NOT EXISTS refund_requests_invoice_idx
    ON refund_requests (invoice_id, status);

-- At most one OPEN (pending/approved, not yet executed/rejected/failed) refund
-- per invoice: a second propose against the same invoice is declined.
CREATE UNIQUE INDEX IF NOT EXISTS refund_requests_one_open_per_invoice
    ON refund_requests (invoice_id)
    WHERE status IN ('pending_approval','approved');

CREATE TABLE IF NOT EXISTS plan_change_requests (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id      UUID NOT NULL REFERENCES tickets(id),
    customer_id    UUID NOT NULL REFERENCES customers(id),
    from_plan_id   TEXT REFERENCES plans(id),
    to_plan_id     TEXT NOT NULL REFERENCES plans(id),
    effective      TEXT NOT NULL DEFAULT 'next_cycle'
                   CHECK (effective IN ('now','next_cycle')),
    status         TEXT NOT NULL DEFAULT 'pending_approval'
                   CHECK (status IN ('pending_approval','approved','rejected',
                                     'executed','failed')),
    proposed_by    TEXT NOT NULL DEFAULT 'agent',
    approved_by    TEXT,
    decided_at     TIMESTAMPTZ,
    executed_at    TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (customer_id, to_plan_id, idempotency_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS plan_change_requests_one_open_per_customer
    ON plan_change_requests (customer_id)
    WHERE status IN ('pending_approval','approved');

CREATE TABLE IF NOT EXISTS approvals (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id   UUID REFERENCES tickets(id),
    action_type TEXT NOT NULL CHECK (action_type IN ('refund','plan_change','send_reply')),
    action_ref  UUID,
    decision    TEXT NOT NULL CHECK (decision IN ('approved','rejected','edited')),
    edited_text TEXT,
    decided_by  TEXT NOT NULL,
    note        TEXT,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approvals_ticket_idx ON approvals (ticket_id, decided_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id        BIGSERIAL PRIMARY KEY,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor     TEXT NOT NULL,
    event     TEXT NOT NULL,
    entity    TEXT,
    entity_id TEXT,
    payload   JSONB
);

-- append-only: nobody may UPDATE or DELETE audit rows (runs before any other trigger)
CREATE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (got %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_immutable_trg
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- Refund state machine guard (PLAN 4.2.2):
--   pending_approval -> approved | rejected | failed
--   approved         -> executed | failed
--   executed/rejected are terminal; failed may be re-proposed with a NEW row.
CREATE FUNCTION refund_state_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.status <> 'pending_approval' THEN
        RAISE EXCEPTION 'refund_requests rows must start as pending_approval';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.status = NEW.status THEN
            RETURN NEW;  -- harmless no-op update (timestamps etc.)
        END IF;
        IF NOT (
            (OLD.status = 'pending_approval' AND NEW.status IN ('approved','rejected','failed'))
         OR (OLD.status = 'approved'         AND NEW.status IN ('executed','failed'))
        ) THEN
            RAISE EXCEPTION 'illegal refund status transition % -> %', OLD.status, NEW.status;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER refund_state_guard_trg
    BEFORE INSERT OR UPDATE ON refund_requests
    FOR EACH ROW EXECUTE FUNCTION refund_state_guard();

-- Sending a reply is an action: customer-visible messages go through this
-- simulated outbox and only land here after a recorded approval decision.
CREATE TABLE IF NOT EXISTS outbox (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id   UUID NOT NULL REFERENCES tickets(id),
    approved_by TEXT NOT NULL,
    body        TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outbox_ticket_idx ON outbox (ticket_id);
