-- 002_core.sql — business core: plans, customers, subscriptions, invoices, payments.
-- Constraints are part of the safety design (PLAN 4.2.2); do NOT weaken them.

CREATE TABLE IF NOT EXISTS plans (
    id                  TEXT PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    monthly_price_cents INTEGER NOT NULL CHECK (monthly_price_cents >= 0),
    annual_price_cents  INTEGER NOT NULL CHECK (annual_price_cents >= 0),
    features            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS customers (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_ref TEXT UNIQUE,
    first_name   TEXT NOT NULL,
    last_name    TEXT NOT NULL,
    email        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','suspended','closed')),
    language     TEXT NOT NULL DEFAULT 'en',
    plan_id      TEXT REFERENCES plans(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Duplicate emails are allowed ONLY as a deliberate flagged edge case;
-- data entry paths must treat a second row with (email) as AMBIGUOUS.
CREATE INDEX IF NOT EXISTS customers_email_idx ON customers (lower(email));

CREATE TABLE IF NOT EXISTS subscriptions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id          UUID NOT NULL REFERENCES customers(id),
    plan_id              TEXT NOT NULL REFERENCES plans(id),
    status               TEXT NOT NULL
                         CHECK (status IN ('active','past_due','canceled','trialing')),
    billing_cycle        TEXT NOT NULL CHECK (billing_cycle IN ('monthly','annual')),
    current_period_start TIMESTAMPTZ,
    current_period_end   TIMESTAMPTZ,
    canceled_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS subscriptions_customer_idx ON subscriptions (customer_id, status);

CREATE TABLE IF NOT EXISTS invoices (
    id              TEXT PRIMARY KEY,
    customer_id     UUID NOT NULL REFERENCES customers(id),
    subscription_id UUID REFERENCES subscriptions(id),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    currency        TEXT NOT NULL DEFAULT 'USD',
    status          TEXT NOT NULL
                    CHECK (status IN ('open','paid','void','refunded','partially_refunded')),
    issued_at       TIMESTAMPTZ NOT NULL,
    due_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS invoices_customer_idx ON invoices (customer_id, status);

CREATE TABLE IF NOT EXISTS payments (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id             TEXT NOT NULL REFERENCES invoices(id),
    amount_cents           INTEGER NOT NULL CHECK (amount_cents > 0),
    method                 TEXT NOT NULL,
    status                 TEXT NOT NULL
                           CHECK (status IN ('succeeded','failed','pending')),
    paid_at                TIMESTAMPTZ,
    amount_refunded_cents  INTEGER NOT NULL DEFAULT 0
                           CHECK (amount_refunded_cents >= 0),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A payment can never be refunded beyond what was paid (money-consistency floor).
ALTER TABLE payments
    ADD CONSTRAINT payments_refund_cannot_exceed_paid
    CHECK (amount_refunded_cents <= amount_cents);

CREATE INDEX IF NOT EXISTS payments_invoice_idx ON payments (invoice_id, status);
