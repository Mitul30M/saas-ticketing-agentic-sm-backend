# Nimbus — agentic support ticket triage with an approval gate

Backend for a fictional SaaS ("Nimbus"): an agent reads a support ticket, retrieves grounding from the
KB, and then **stops**. Anything that changes money, a plan, or talks to a customer waits for a human
decision in the dashboard. The LLM proposes; deterministic code disposes.

- **Stack:** Python 3.14 · FastAPI · Pydantic v2 · LangGraph (+ Postgres checkpointer) · psycopg 3 ·
  PostgreSQL 16 with pgvector · `sentence-transformers` (`BAAI/bge-small-en-v1.5`, CPU) · Groq or a
  deterministic stub LLM.
- **No paid API required:** everything runs locally; the LLM provider is `stub` by default.

## Quickstart (Docker)

```bash
cp .env.example .env            # optional; compose has sane defaults
AGENT_TOKEN=demo-token docker compose up -d --build   # db + api + worker
```

`AGENT_TOKEN` must be passed on **every** `up`: without it the API starts with an empty token and the
dashboard routes fail closed with `503`.

Then seed and load the knowledge base (runs on the host, applies migrations itself):

```bash
make migrate
make seed          # 4 plans, 101 customers, 368 invoices/payments, 16 seed tickets + edge cases
make ingest-kb     # 30 KB articles -> 49 embedded chunks
make load-tickets  # optional: the imported dataset tickets (data/tickets_all.jsonl)
```

Check it:

```bash
curl -s localhost:8000/healthz                     # {"status":"ok"}
curl -s localhost:8000/api/tickets -H "Authorization: Bearer demo-token"
```

- API: <http://localhost:8000> (openapi at `/docs`)
- Postgres: `localhost:5433` (`nimbus`/`nimbus`, database `nimbus`)

### Frontend console (optional, separate repo)

The Next.js console is a **separate repo** (`../saas-ticketing-agentic-sm-next-app`). It is mounted
in this compose file as the `web` service behind a **profile**, because the build context is that
sibling directory — a backend-only clone must still run with plain `docker compose up`.

```bash
AGENT_TOKEN=demo-token docker compose --profile web up -d --build
# or: AGENT_TOKEN=demo-token make up-web
```

- Console: <http://localhost:3000> (public `/submit`; `/dashboard` asks for an operator name)
- The browser never sees `AGENT_TOKEN`: the container proxies to `http://api:8000` server-side.
- If the console repo is not present, `--profile web` is the only thing that fails; the API and
  worker are unaffected. `make up` stays backend-only on purpose.
- The API runs migrations on startup and **preloads the embedding model**; the worker does the same
  before polling, so the first ticket does not hit the 8 s tool timeout on a cold clone.

## Tests

```bash
docker compose stop worker     # REQUIRED: the worker claims the jobs the tests create
make test-all                  # unit + integration + safety (needs the db container)
make lint                      # ruff + mypy
AGENT_TOKEN=demo-token docker compose up -d worker   # restart it afterwards
```

`make test-all` **aborts immediately** with instructions if a worker is connected to the test
database (it detects `application_name=nimbus-worker`), instead of failing randomly. The
integration/safety suites *skip* when Postgres is unreachable, so watch the pass count, not just the
exit code.

`make eval` is scaffolded but `eval/run_eval.py` is not written yet — evaluation numbers are not
published until that lands (Phase 5). Don't read anything into an empty `eval/results/`.

## Where things live

| Path | What |
|---|---|
| `app/agent/` | LangGraph graph, nodes, prompts, checkpointer — never imports `app.services` |
| `app/services/` | approvals, executor, job queue, idempotency, ticket state (business rules) |
| `app/tools/` | narrow typed tools; no raw SQL tool; typed `{ok:false,error:{code,...}}` failures |
| `app/retrieval/` | hybrid FTS + pgvector retrieval with RRF, embedding cache |
| `app/api/` | HTTP boundary, auth, size limits, metrics, dev-only admin routes |
| `db/migrations/` | forward-only SQL; constraints/triggers are part of the safety design |
| `tests/safety/` | AT1–AT5 acceptance tests (approval gate, abstain, injection, tool failure, step limit) |
| `docs/VERIFY.md` | full runbook: every command, every edge case, troubleshooting |
| `docs/ROUTING.MD` | categories, queues, statuses, escalation reasons, dashboard tabs |
| `docs/DECISIONS.md` | why each non-obvious choice was made |

## Safety invariants (short version)

1. `propose_refund` / `propose_plan_change` only insert `pending_approval`; only
   `services/approvals.py` + `services/executor.py` may move them forward, after a recorded human
   decision. Sending a reply is an action too (`outbox`, also approval-gated).
2. The approval gate exists in the database (trigger + `approved_by` / `executed_at`), not only in
   code; a bypassing `UPDATE` raises.
3. Ticket text is untrusted data: never in system prompts, and injection detection may only *tighten*
   behaviour (force human review).
4. Abstain over guess: a reply needs retrieval above the calibrated threshold, a claim-level support
   check, and every `[KB-xxx]` citation inside the retrieved set — otherwise escalate with a reason.
5. Business time comes from `AS_OF`, never the wall clock; every run is traced and audited.

Read `AGENTS.md` for the full rules and `PLAN.md` for scope and phase status.
