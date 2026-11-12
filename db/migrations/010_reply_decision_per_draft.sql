-- Bind reply decisions to the exact draft shown to the operator.
ALTER TABLE approvals
    ADD COLUMN IF NOT EXISTS draft_hash TEXT;

DROP INDEX IF EXISTS approvals_one_final_decision;

CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_final_action_decision
    ON approvals (action_type, action_ref)
    WHERE action_type <> 'send_reply'
      AND action_ref IS NOT NULL
      AND decision IN ('approved','rejected');

CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_reply_decision_per_draft
    ON approvals (action_type, action_ref, draft_hash)
    WHERE action_type = 'send_reply'
      AND action_ref IS NOT NULL
      AND draft_hash IS NOT NULL
      AND decision IN ('approved','rejected');
