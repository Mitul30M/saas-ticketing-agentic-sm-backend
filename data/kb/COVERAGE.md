# Nimbus KB coverage manifest

This manifest is the ground truth for whether the first Phase 1 KB should support a ticket. Article IDs are the expected citation set for the covered cases; an uncovered topic must escalate rather than receive a guessed answer.

## Covered topics

| Topic | Articles | Coverage decision |
|---|---|---|
| `invoice_question` | KB-001–KB-005 | Cover invoice status, payment status, payment method, and amount discrepancies. |
| `plan_change` | KB-010–KB-016 | Cover plan selection, upgrades, downgrades, billing cycles, cancellation, trials, and proration. |
| `login_account_access` | KB-017–KB-020 | Cover password reset, SSO, MFA recovery, and billing-contact changes. |
| `api_integration` | KB-021–KB-024 | Cover API keys, rate limits, webhooks, and integration setup. |
| `sync_issues` | KB-025–KB-028 | Cover delayed syncs, duplicates, exports, and integration permissions. |
| `service_outage` | KB-029–KB-030 | Cover evidence collection and status-page checks only; incident-specific promises remain unsupported. |
| refund scenarios | KB-006–KB-009 | Cover policy, eligibility, proration limits, and status. Refund execution always remains approval-gated. |

## Deliberately uncovered

| Topic or case | Expected behavior |
|---|---|
| `security_incident` | Escalate to a human security responder. The KB intentionally contains no security-incident runbook. |
| `feature_request` | Escalate; feature commitments and roadmap answers are not supported. |
| Fax, paper-invoice, and physical-document requests | Escalate; no article claims that Nimbus supports these channels. |
| A slice of `service_outage` involving an active, incident-specific outage | Escalate unless retrieved evidence directly supports the exact incident. |

## Retrieval notes

KB-006 and KB-007 are a near-duplicate pair with different update dates. KB-029 and KB-030 overlap on outage triage but have different evidence boundaries. KB-001/KB-004 and KB-011/KB-013 intentionally share billing language so retrieval must use the ticket details rather than a keyword-only match.

The articles are synthetic Nimbus content. Drafting assistance was used and the corpus was reviewed for the stated coverage and safety boundaries; see `docs/AI_USAGE.md`.
