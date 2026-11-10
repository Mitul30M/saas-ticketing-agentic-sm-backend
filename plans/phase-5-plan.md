# Phase 5 — Frontend (⚠ out of scope for this repo)

> Source: `PLAN.md` Part 7, Phase 5.
> **Status: DONE IN A SEPARATE REPO.** The Next.js frontend (customer portal `/submit`, agent dashboard `/dashboard`, ticket detail, approval panel, metrics page) lives in its own repository. This file is kept only as a reference/spec so the backend exposes everything the UI needs.

## II. UI requirements the backend must support (reference)

- `/submit` form with validation + `Idempotency-Key` per submit + success state — served by `POST /api/tickets`.
- `/dashboard` list with filters/tabs/pagination (`GET /api/tickets?status=&queue=&escalated=&q=&page=`); polling or SSE.
- Ticket detail: triage + reasons, draft with **clickable KB citations**, tool evidence, injection banner, trace timeline (`GET /api/tickets/{id}`, `GET /api/tickets/{id}/trace`).
- **Approval panel:** shows proposed action, amount, invoice, evidence (tool outputs + KB IDs); Approve/Edit/Reject; disabled after decision (`GET /api/approvals/pending`, `POST /api/approvals/{action_id}/decide`).
- `/dashboard/metrics` (counts, escalation rate, latency, cost, latest eval results) — `GET /api/metrics/summary`.
- Seed/demo script prints walkthrough ticket IDs (one per scenario) for the video.

## Notes & key facts

- Backend contract is PLAN §4.9 — do not change public API shapes without updating the frontend repo's types AND PLAN §4.9.
- Approval panel must show **action + amount + evidence** before enabling Approve.
- Auth is minimal: shared agent token via env — documented as a known limit.

## Log & achievements (append)

- 2026-11-10 — Owner confirmed frontend is a separate repo; this phase is deferred/external. Backend API surface becomes the contract (PLAN §4.9).
