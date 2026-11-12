-- 009_plan_change_schedule.sql — persist deferred plan changes.
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS scheduled_plan_id TEXT REFERENCES plans(id),
    ADD COLUMN IF NOT EXISTS scheduled_effective_at TIMESTAMPTZ;
