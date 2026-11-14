# Design note — Nimbus agentic support triage

**Scope.** A support agent for a fictional SaaS: triage a ticket, retrieve grounded knowledge, answer
when the evidence supports it, and propose refunds or plan changes that a human must approve. The
goal is *safe automation*, not autonomy — the interesting engineering is the boundary between what
the model may suggest and what the system may do.

## Design choices

**A workflow graph, not an autonomous tool loop.** The branch (reply / refund / plan change) is
policy, so deterministic code selects it from the triage category. The model is used only for
classification, the support check and customer-facing prose. Every node has one job, a step limit and
a terminal escalation path.

**Approval is enforced twice.** Tools only insert `pending_approval` rows; a separate service layer
moves them forward after a recorded human decision. Database triggers require an `approvals` row and
`approved_by` before approval, and `executed_at` before execution. A coding mistake cannot bypass the
gate with an `UPDATE`. Sending a reply is an action too (`outbox`).

**Evidence before action.** A reply needs retrieval above a calibrated threshold, an LLM support
check whose claimed `kb_id`s are validated in code against the retrieved set, every inline citation
inside that set, and a deterministic promise-language check. A refund also needs ownership from the
transport sender, a successful payment, the remaining balance and the 30-day window — all checked
server-side in the tool. Any failure escalates with a reason.

**Read the record before answering.** Invoice tickets read the invoice first and receive a
whitelisted `ACCOUNT_RECORD` (status, amounts, refunded amounts, payment method/date); the draft must
state those facts and may not ask for them again. Refund drafts see the amount about to be proposed
only when the tool's preconditions say it will be accepted. Account tickets read the sender's status
to explain an escalation, but the status is never passed to the draft or used to authorise a change.

**Ticket text and identity are untrusted.** Ticket text never enters system prompts; it is passed in
delimited user content. Injection screening only adds flags and can only tighten behaviour. Identity
comes from `ticket.from_email`; an email, customer id or invoice id in the body is a claim to verify,
never a lookup key.

**Deterministic time, idempotency and traces.** Refund windows use the injected `AS_OF`, never the
wall clock, so labels stay stable. Mutating endpoints require idempotency keys; refund/plan tools
have unique constraints and the executor locks rows with `FOR UPDATE`. Every node, tool, LLM call,
refusal and decision writes a trace row; the audit log is append-only. The console shows the action,
amount, evidence, draft and the reason a run was parked.

**Evaluation discipline.** The 43-row eval set is disjoint from the 24-row dev set, every row is
hand-reviewed, and gold is read only by `eval/`. The final capture uses a real model
(`openai/gpt-oss-120b`) and is replayable from a recorded cache. Failures are documented with traces
rather than relabelled.

## Cost per 1,000 tickets

The published capture processed 43 tickets with 92,353 input and 47,196 output tokens: **3,245
tokens and 2.37 LLM calls per ticket**.

| Measure | Value |
|---|---|
| Actual measured cost | **$0.00 / 1,000 tickets** — Groq free tier |
| Shadow cost at `gpt-4o-mini` reference rates ($0.15/M in, $0.60/M out) | **$0.98 / 1,000 tickets** |
| Latency | **4.59 s p50 / 6.35 s p95** |

The shadow figure is the honest paid-API estimate: 92,353 × $0.15/M + 47,196 × $0.60/M = **$0.042
for 43 tickets**. Prompt/context size dominates the bill — retrieval top-k, the record block and the
support prompt matter more than ticket count. Output is bounded by the 150-word draft. A smaller
triage model or routing simple intents to a cheaper model would reduce this materially; neither was
needed for the free-tier run.

## Limits

- **KB vocabulary is a real ceiling.** `E-008` ("past due", "flagged") over-escalates because no
  article uses the customer's words, even though `KB-002`/`KB-003` cover the concept. That is a
  content fix, queued with the next capture.
- **Triage is probabilistic.** Sentiment is weakest (0.732; neutral/negative boundary) and priority
  is weakest on the harder dev split. Groq at temperature 0 can still move a label between runs.
- **One false answer is a label-policy question.** `E-034` gets generically true advice for a
  third-party pair with no article (F14). The guide must decide whether that counts as "not in KB".
- **No account-state or cancellation tools.** A suspended customer is escalated, not reactivated;
  the status is now in the reason. There is no cancellation action, and none should be added without
  an approval-gated design.
- **Single-turn, text-only.** No thread memory beyond the ticket, no attachments, no multi-message
  negotiation; the KB is a fixed synthetic corpus.
- **Demo-grade edges.** Operator sign-in is a name label, not authentication; email is simulated via
  `outbox`; data is synthetic; the free Groq tier allows roughly one full capture per day. Changing a
  prompt or the KB invalidates the replay cache.

## Next steps

1. **Recapture on a fresh budget** with the KB-002/003 vocabulary fix and F18/F19/F22, then regenerate
   `docs/EVAL_RESULTS.md`. That converts the honest "HEAD misses the frozen replay" note into a new
   table.
2. **Decide F14** (generic advice vs product-specific evidence) and update the guide and support
   prompt together, so the metric and the label cannot disagree.
3. **Add a suspension runbook** and a read-only "what the record shows" answer, keeping account-state
   changes behind a human and a separate tool.
4. **Finish the dev triage labels / `triage_v4`** for mitigated outages and informational billing,
   measured with `make dev-triage` before any recapture.
5. **Harden operations**: real operator identity/roles, a real email provider behind the approval
   gate, managed Postgres and secret storage.
6. **Broaden the eval** to multi-turn threads, attachments and per-category slices; keep the current
   set as the regression core and report uncertainty as it grows.
