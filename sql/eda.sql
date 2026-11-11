-- Nimbus dataset/database EDA queries.
-- Run these read-only queries in DataGrip against the `nimbus` database.
-- The file is intentionally a collection of separate result sets.

-- 1. Overall table sizes
SELECT 'plans' AS table_name, COUNT(*) AS row_count FROM plans
UNION ALL SELECT 'customers', COUNT(*) FROM customers
UNION ALL SELECT 'subscriptions', COUNT(*) FROM subscriptions
UNION ALL SELECT 'invoices', COUNT(*) FROM invoices
UNION ALL SELECT 'payments', COUNT(*) FROM payments
UNION ALL SELECT 'tickets', COUNT(*) FROM tickets
UNION ALL SELECT 'refund_requests', COUNT(*) FROM refund_requests
UNION ALL SELECT 'plan_change_requests', COUNT(*) FROM plan_change_requests
UNION ALL SELECT 'approvals', COUNT(*) FROM approvals
UNION ALL SELECT 'outbox', COUNT(*) FROM outbox
UNION ALL SELECT 'trace_events', COUNT(*) FROM trace_events
UNION ALL SELECT 'idempotency_keys', COUNT(*) FROM idempotency_keys
ORDER BY table_name;

-- 2. Plans, pricing, and features
SELECT id, code, name, monthly_price_cents / 100.0 AS monthly_price,
       annual_price_cents / 100.0 AS annual_price, features
FROM plans
ORDER BY monthly_price_cents;

-- 3. Customer status, plan, language, and duplicate-email checks
SELECT status, language, COUNT(*) AS customers
FROM customers
GROUP BY status, language
ORDER BY status, language;

SELECT p.code AS plan, c.status, COUNT(*) AS customers
FROM customers c
LEFT JOIN plans p ON p.id = c.plan_id
GROUP BY p.code, c.status
ORDER BY p.code, c.status;

SELECT lower(email) AS email, COUNT(*) AS customer_rows,
       ARRAY_AGG(external_ref ORDER BY external_ref) AS customer_refs
FROM customers
GROUP BY lower(email)
HAVING COUNT(*) > 1
ORDER BY customer_rows DESC, email;

-- 4. Subscriptions and billing cycles
SELECT status, billing_cycle, COUNT(*) AS subscriptions
FROM subscriptions
GROUP BY status, billing_cycle
ORDER BY status, billing_cycle;

SELECT p.code AS plan, s.status, s.billing_cycle, COUNT(*) AS subscriptions
FROM subscriptions s
JOIN plans p ON p.id = s.plan_id
GROUP BY p.code, s.status, s.billing_cycle
ORDER BY p.code, s.status, s.billing_cycle;

-- 5. Invoice counts, amounts, statuses, and deterministic ages
SELECT status, COUNT(*) AS invoices,
       ROUND(AVG(amount_cents) / 100.0, 2) AS avg_amount,
       ROUND(SUM(amount_cents) / 100.0, 2) AS total_amount
FROM invoices
GROUP BY status
ORDER BY status;

SELECT MIN(issued_at) AS oldest_invoice, MAX(issued_at) AS newest_invoice,
       MIN(EXTRACT(EPOCH FROM (TIMESTAMPTZ '2026-11-10T12:00:00Z' - issued_at)) / 86400) AS min_age_days,
       MAX(EXTRACT(EPOCH FROM (TIMESTAMPTZ '2026-11-10T12:00:00Z' - issued_at)) / 86400) AS max_age_days
FROM invoices;

SELECT CASE
         WHEN issued_at > TIMESTAMPTZ '2026-11-10T12:00:00Z' - INTERVAL '30 days'
           THEN 'within_30_day_window'
         ELSE 'outside_30_day_window'
       END AS refund_window,
       COUNT(*) AS invoices
FROM invoices
WHERE status IN ('paid', 'partially_refunded', 'refunded')
GROUP BY refund_window;

-- 6. Payments, payment methods, failures, and refundable balances
SELECT status, method, COUNT(*) AS payments,
       ROUND(SUM(amount_cents) / 100.0, 2) AS paid_amount,
       ROUND(SUM(amount_refunded_cents) / 100.0, 2) AS refunded_amount
FROM payments
GROUP BY status, method
ORDER BY status, method;

SELECT i.id AS invoice_id, c.external_ref, i.amount_cents / 100.0 AS invoice_amount,
       COALESCE(SUM(p.amount_cents) FILTER (WHERE p.status = 'succeeded'), 0) / 100.0
         AS succeeded_amount,
       COALESCE(SUM(p.amount_refunded_cents), 0) / 100.0 AS refunded_amount,
       (COALESCE(SUM(p.amount_cents) FILTER (WHERE p.status = 'succeeded'), 0)
        - COALESCE(SUM(p.amount_refunded_cents), 0)) / 100.0 AS remaining_refundable
FROM invoices i
JOIN customers c ON c.id = i.customer_id
LEFT JOIN payments p ON p.invoice_id = i.id
GROUP BY i.id, c.external_ref, i.amount_cents
ORDER BY remaining_refundable DESC, i.id;

-- 7. Agent labels and ticket quality.
-- Source-file `topic`, `type_hint`, and `queue_hint` distributions are in
-- data/tickets_all.jsonl; the database stores the agent's eventual labels.
SELECT category, priority, sentiment, queue, COUNT(*) AS tickets
FROM tickets
GROUP BY category, priority, sentiment, queue
ORDER BY tickets DESC;

SELECT reviewed, source, COUNT(*) AS tickets
FROM tickets
GROUP BY reviewed, source
ORDER BY reviewed, source;

SELECT COUNT(*) FILTER (WHERE from_email IS NULL) AS missing_sender,
       COUNT(*) FILTER (WHERE subject IS NULL OR btrim(subject) = '') AS missing_subject,
       COUNT(*) FILTER (WHERE body IS NULL OR btrim(body) = '') AS missing_body,
       COUNT(*) AS total_tickets
FROM tickets;

SELECT t.id, c.external_ref, t.from_email, t.subject, t.received_at
FROM tickets t
LEFT JOIN customers c ON c.id = t.customer_id
WHERE from_email IS NULL
   OR subject IS NULL OR btrim(subject) = ''
ORDER BY received_at;

-- 8. Ticket-to-customer distribution and source metadata
SELECT c.external_ref, c.email, COUNT(t.id) AS tickets
FROM customers c
LEFT JOIN tickets t ON t.customer_id = c.id
GROUP BY c.external_ref, c.email
ORDER BY tickets DESC, c.external_ref;

SELECT source, queue, category, COUNT(*) AS tickets
FROM tickets
GROUP BY source, queue, category
ORDER BY source, tickets DESC;

-- 9. Refund and plan-change proposals
SELECT status, COUNT(*) AS refund_requests,
       ROUND(SUM(amount_cents) / 100.0, 2) AS requested_amount
FROM refund_requests
GROUP BY status
ORDER BY status;

SELECT rr.id, rr.invoice_id, c.external_ref, rr.amount_cents / 100.0 AS amount,
       rr.status, rr.approved_by, rr.created_at
FROM refund_requests rr
JOIN tickets t ON t.id = rr.ticket_id
JOIN customers c ON c.id = t.customer_id
ORDER BY rr.created_at DESC;

SELECT status, effective, COUNT(*) AS plan_changes
FROM plan_change_requests
GROUP BY status, effective
ORDER BY status, effective;

-- 10. Approvals, outbox, traces, and failures
SELECT action_type, decision, COUNT(*) AS decisions
FROM approvals
GROUP BY action_type, decision
ORDER BY action_type, decision;

SELECT COUNT(*) AS sent_replies,
       MIN(delivered_at) AS first_delivery,
       MAX(delivered_at) AS last_delivery
FROM outbox;

SELECT node, kind, status, COUNT(*) AS events,
       ROUND(AVG(duration_ms), 2) AS avg_duration_ms,
       SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out,
       ROUND(SUM(cost_usd), 6) AS cost_usd
FROM trace_events
GROUP BY node, kind, status
ORDER BY events DESC, node;

SELECT status, COUNT(*) AS jobs,
       MIN(created_at) AS first_created, MAX(created_at) AS last_created
FROM jobs
GROUP BY status
ORDER BY status;

-- 11. Edge-case tickets useful for demos
SELECT id, subject, from_email, customer_id, status, escalated, escalation_reason
FROM tickets
WHERE subject ILIKE ANY (ARRAY[
    '%refund%', '%invoice%', '%past due%', '%suspend%', '%injection%',
    '%system directive%', '%annual%', '%downgrade%'
])
ORDER BY received_at;
