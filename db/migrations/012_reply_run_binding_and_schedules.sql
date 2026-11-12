-- 012_reply_run_binding_and_schedules.sql — close two Phase 4 gaps found in review.

-- 1. Bind a reply decision to the RUN that produced the draft.
-- A rejected draft followed by a re-run may legitimately produce the SAME text
-- (the LLM cache makes it likely). Uniqueness on the draft hash alone would then
-- reject the second decision as a duplicate. Uniqueness is therefore
-- (action, ticket, run_key, draft_hash): the same draft text can be decided once
-- per run, and a replay inside one run stays idempotent.
ALTER TABLE approvals
    ADD COLUMN IF NOT EXISTS run_key TEXT;

DROP INDEX IF EXISTS approvals_one_reply_decision_per_draft;

CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_reply_decision_per_run_draft
    ON approvals (action_type, action_ref, run_key, draft_hash)
    WHERE action_type = 'send_reply'
      AND action_ref IS NOT NULL
      AND run_key IS NOT NULL
      AND draft_hash IS NOT NULL
      AND decision IN ('approved','rejected');

-- 2. A deferred plan change is written to the subscription immediately
-- (scheduled_plan_id). Keep a durable link back to the approved request so the
-- worker can apply it at period end and audit exactly once.
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS scheduled_plan_change_id UUID REFERENCES plan_change_requests(id);
