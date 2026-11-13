# Routing map — categories, queues, statuses, and approval surfaces

Source of truth: `app/domain/enums.py` (taxonomy + `CATEGORY_QUEUE`), the `tickets` CHECK in
`db/migrations/003_support.sql`, and the escalation reason codes in `app/agent/nodes/*.py`.
If this file and the code disagree, the code wins — update this file in the same PR.

## 1. Categories (10) — proposed by the LLM, validated against the enum

| Category | Typical problem types |
|---|---|
| `invoice_question` | charge looks wrong, invoice/receipt copy, payment status, payment method, tax/VAT, billing-cycle question |
| `refund_request` | refund inside/outside the 30-day window, duplicate-charge refund, cancellation refund |
| `plan_change` | upgrade/downgrade, seat changes, monthly↔annual, trial→paid, proration |
| `login_account_access` | password reset, MFA/2FA recovery, SSO login, locked/disabled account, billing-contact change |
| `api_integration` | API keys, rate limits, webhooks, SDK/auth errors, integration setup |
| `sync_issues` | data not syncing, stale dashboards, delayed exports, duplicate syncs, connector permissions |
| `service_outage` | platform down, 5xx, degraded performance, active incident, status-page questions |
| `security_incident` | suspicious login, phishing report, credential leak, data exposure, unauthorized access |
| `feature_request` | new feature / integration / roadmap asks |
| `other` | does not fit, or genuinely ambiguous |

## 2. Queues (7) — assigned by CODE, never by the model

`CATEGORY_QUEUE` in `app/domain/enums.py`; that dict is the single edit point for routing.

| Queue | Receives | Team |
|---|---|---|
| `billing` | `invoice_question`, `refund_request`, `plan_change` | billing / collections |
| `account` | `login_account_access` | identity & account support |
| `technical` | `api_integration`, `sync_issues` | integrations / engineering support |
| `security` | `security_incident` | security responders (KB intentionally has no runbook) |
| `incidents` | `service_outage` | on-call / incident commander |
| `product` | `feature_request` | product / roadmap triage |
| `general` | `other` | catch-all / unrouted |

- Escalation **keeps** the triage queue: an escalated refund stays in `billing`, a security incident
  stays in `security`. `escalate_to_human` does not rewrite the queue.
- Escalation *before* triage (bad ticket, ticket not found) leaves `queue = NULL` → render
  "unclassified".
- Seed/imported tickets have `category`, `queue`, `priority`, `sentiment` all `NULL` until an agent
  run completes.

## 3. Priority (4) and sentiment (3)

From `app/agent/prompts/triage_v1.txt`: `urgent` only for an active outage or an active security
incident; `high` for money at risk (double charge, failed payment on an active plan); `medium`
default; `low` for feature requests / how-to questions. Sentiment:
`positive | neutral | negative`.

## 4. Statuses (DB CHECK constraint)

| status | Meaning | Human-actionable |
|---|---|---|
| `new` | created, job queued | no (still processing) |
| `triaging` | agent running | no |
| `awaiting_approval` | paused at the reply gate with a draft | **yes** — approve / edit / reject |
| `awaiting_human` | escalated, abstained, or job failed | **yes** |
| `resolved` | approved reply written to `outbox` | no |
| `failed` | reserved; real failures park as `awaiting_human` + `agent_outcome='failed'` | filter on `agent_outcome` |

`agent_outcome`: `action_proposed | escalated | failed | draft_ready` (the last is allowed but
unused). `handled_by`: `unassigned | agent | human` (`human` is allowed by the DB but nothing sets it
yet — the dashboard should set it when an admin claims a ticket).

## 5. Two approval surfaces (both feed the "Awaiting approval" tab)

1. **Reply gate** — `tickets.status='awaiting_approval'` with `draft_reply`; decide via
   `POST /api/approvals/send_reply/{ticket_id}/decide`.
2. **Action gate** — a `refund_requests` or `plan_change_requests` row in `pending_approval`; decide
   via `POST /api/approvals/{refund|plan_change}/{request_id}/decide`.

`GET /api/approvals/pending` merges all three kinds (`send_reply`, `refund`, `plan_change`) with
subject, draft, citations, flags, the injection flag, and amount/invoice/plan fields. One ticket can
hold a pending reply **and** a pending action → dedupe/group by ticket in the UI.

Reply decisions are bound to the agent **run** (`approvals.run_key` + the run/draft unique index):
the same draft from the same run is decidable once, and a replay returns the recorded decision.

## 6. Escalation reasons (`tickets.escalation_reason`)

| Reason | When |
|---|---|
| `LOW_CONFIDENCE` | best retrieval score < τ, or the claim-level support check fails |
| `CONFLICTING_SOURCES` | KB articles conflict and there is no unique newest policy |
| `DRAFT_CITATION_INVALID` | draft cites a `KB-xxx` that was not retrieved |
| `DRAFT_PROMISE_DENIED` | draft promises an action (deny list) |
| `NEEDS_HUMAN_JUDGEMENT` | ambiguous amount/plan, malformed `$` amount, refund with no verifiable invoice |
| `DRAFT_REJECTED` | human rejected the reply draft |
| `STEP_LIMIT` | step budget exhausted |
| `INTERNAL_ERROR` | unexpected node exception (fail-closed) |
| `AGENT_ESCALATED` | generic fallback when no reason was set |
| `BAD_TICKET` / `TICKET_NOT_FOUND` | intake validation failed (queue stays `NULL`) |
| `JOB_FAILED` | worker exhausted attempts; ticket parked for a human |
| `TOOL_FAILURE` or a typed tool code: `ownership_mismatch`, `not_found`, `business_rule`, `duplicate`, `invalid_input`, `timeout`, `internal` | a tool returned `{ok:false}`; identity/amount/invoice probes land here |

## 7. What `GET /api/tickets` supports today

Filters: `status`, `queue`, `escalated`, `handled_by`, `q` (subject/body ILIKE), `page`, `limit`.
Sort is always `received_at DESC`.

So **category, priority, sentiment, and cross-dataset grouping cannot be filtered server-side yet**;
the table can only sort/group the current page client-side. The planned extension (needs a `PLAN.md`
§4.9 update + frontend types in the same PR):

- filters: `category`, `priority`, `sentiment`, `injection_suspected`, `agent_outcome`,
  `has_pending_approval`, `approval_kind`;
- `sort=received_at|priority|status` + `order=asc|desc`;
- `GET /api/tickets/facets` for per-category/queue/priority/status counts. Tab badges already work
  from `GET /api/metrics/summary`, which returns `tickets_by_category`, `tickets_by_queue`, and
  `escalations_by_reason`.

## 8. Suggested dashboard tabs

| Tab | Filter |
|---|---|
| All | — |
| Awaiting approval | `status=awaiting_approval` OR a pending action (`/api/approvals/pending`) |
| Escalated / needs human | `status=awaiting_human` (or `escalated=true`) |
| Resolved | `status=resolved` |
| Failed runs | `agent_outcome=failed` (not `status=failed`) |
| Unclassified | `category IS NULL` (needs the new filter) |
