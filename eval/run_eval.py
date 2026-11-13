"""End-to-end evaluation runner (PLAN 4.11 / Phase 6).

    uv run python -m eval.run_eval --llm stub --limit 3
    uv run python -m eval.run_eval --llm groq --write-replay

What it does:

1. Creates/uses an isolated database (`nimbus_eval` by default), runs the real
   migrations, the real deterministic seed and the real KB ingest.
2. Inserts every eval ticket the way production intake does (`create_ticket`),
   then drives the real worker (`worker.run_one`): jobs table -> graph ->
   approval gate. It never calls an approval/decide endpoint, so every run
   stops at the gate.
3. Asserts the safety invariants on the resulting database and refuses to write
   results if anything was approved, executed or sent.
4. Writes `eval/results/<run>.json`: the manifest (model, commit, dataset hash,
   prompt versions, AS_OF), one record per ticket with gold + actual + trace
   summary, and the safety counters.

Gold is read *here*, after the run, and is never passed to the agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from app import db
from app.config import settings
from app.db import create_pool
from app.llm.base import LLMClient, LLMError
from app.retrieval.hybrid import warm_encoder
from app.services.intake import create_ticket
from app.worker import run_one

EVAL_SET = Path("data/eval/eval_set.jsonl")
RESULTS_DIR = Path("eval/results")
REPLAY_PATH = Path("eval/replay_cache.json")
DEFAULT_DB = "postgresql://nimbus:nimbus@localhost:5433/nimbus_eval"


# --- database plumbing ------------------------------------------------------


def _dbname(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/").split("?")[0]


def _maintenance_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    return f"{parts.scheme}://{parts.netloc}/postgres"


def _prepare_database(dsn: str, *, fresh: bool) -> None:
    """Create the eval database (and optionally drop it first).

    Refuses any name that does not end in `_eval` unless the caller overrides,
    so a typo can never wipe the demo database.
    """
    name = _dbname(dsn)
    with psycopg.connect(_maintenance_dsn(dsn), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if fresh and exists:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            exists = None
        if not exists:
            conn.execute(f'CREATE DATABASE "{name}"')


def _prepare_schema(dsn: str) -> None:
    conn = db.connect(dsn)
    try:
        applied = db.run_migrations(conn)
        print(f"  migrations: {len(applied)} applied (schema up to date)", flush=True)
    finally:
        conn.close()


def _seed_and_ingest() -> None:
    from db.seed.seed import main as seed_main
    from scripts.ingest_kb import KB_DIR, ingest

    seed_main()
    articles, chunks = ingest(KB_DIR, settings.embed_model)
    print(f"  kb: {articles} articles / {chunks} chunks", flush=True)


# --- tickets ----------------------------------------------------------------


def _insert_ticket(pool: Any, row: dict[str, Any]) -> str:
    """Insert through the production path; a missing sender is inserted directly.

    `create_ticket` rejects a null sender because the public form requires an
    email. A transport without a sender is still a real ticket (the seed creates
    them too), and it is exactly the BAD_TICKET case the eval must measure, so
    it takes the same INSERT + job enqueue with a NULL `from_email`.
    """
    ticket = row["ticket"]
    if ticket["from_email"]:
        created = create_ticket(pool, {
            "subject": ticket["subject"],
            "body": ticket["body"],
            "from_email": ticket["from_email"],
            "from_name": ticket["from_name"],
        })
        if created["duplicate"]:
            raise RuntimeError(f"{row['id']}: eval tickets must not dedupe")
        ticket_id = str(created["ticket_id"])
    else:
        with pool.connection() as conn, conn.transaction():
            inserted = conn.execute(
                "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, "
                "from_name, channel, source, status) "
                "VALUES (NULL,%s,%s,%s,NULL,%s,%s,'eval','new') RETURNING id",
                (
                    ticket["subject"], ticket["body"],
                    hashlib.sha256(ticket["body"].encode()).hexdigest(),
                    ticket["from_name"], ticket.get("channel") or "email",
                ),
            ).fetchone()
            ticket_id = str(inserted["id"])
            conn.execute(
                "UPDATE tickets SET thread_id=%s WHERE id=%s",
                (f"ticket:{ticket_id}:run:1", ticket_id),
            )
            conn.execute(
                "INSERT INTO jobs (ticket_id, kind, payload, dedupe_key) "
                "VALUES (%s,'run_agent','{}',%s) ON CONFLICT DO NOTHING",
                (ticket_id, f"run_agent:{ticket_id}:1"),
            )
    # Deterministic business time: received_at is AS_OF - the labelled age, so
    # labels stay stable no matter when the run happens. (now() is audit-only.)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tickets SET received_at = %s - (%s::float8 * interval '1 hour') "
            "WHERE id=%s",
            (settings.as_of, float(ticket["received_hours_before_as_of"]), ticket_id),
        )
    return ticket_id


def _pending_jobs(pool: Any) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()
    return int(row["n"])


def _drain(pool: Any, client: LLMClient, concurrency: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    stop = threading.Event()

    def worker(index: int) -> None:
        worker_id = f"eval-{index}"
        while not stop.is_set() and time.monotonic() < deadline:
            if run_one(pool, worker_id, client=client):
                continue
            if _pending_jobs(pool) == 0:
                stop.set()
                return
            # a job is scheduled for a retry backoff; wait instead of spinning
            time.sleep(0.3)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(concurrency)]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=max(1.0, deadline - time.monotonic()))
    finally:
        stop.set()
    if _pending_jobs(pool) > 0:
        raise RuntimeError(
            f"eval did not drain within {timeout_s:.0f}s; {_pending_jobs(pool)} job(s) still pending"
        )


class _PacedClient:
    """Keep the run inside a provider's per-minute request *and* token budget.

    A 43-ticket run makes ~130 LLM calls. Pacing by request count alone still
    tripped Groq's per-minute token limit, because grounding prompts carry five
    KB chunks (~2.5k tokens) while triage prompts are ~700: the same call rate
    costs very different tokens. This tracks the tokens actually returned in a
    sliding 60 s window and waits before the next call. Calls are serialised
    while held, so the budget is exact and no two threads overshoot together.
    """

    def __init__(self, inner: Any, rpm: float = 0.0, tpm: float = 0.0) -> None:
        self._inner = inner
        self._rpm = rpm
        self._tpm = tpm
        self._lock = threading.Lock()
        self._calls: deque[tuple[float, int]] = deque()
        self._last_start = 0.0

    @property
    def provider(self) -> str:
        return str(getattr(self._inner, "provider", "unknown"))

    @property
    def misses(self) -> int:
        return int(getattr(self._inner, "misses", 0))

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self._wait_for_budget()
            self._last_start = time.monotonic()
            response = self._inner.generate(*args, **kwargs)
            usage = getattr(response, "usage", None)
            tokens = int(getattr(usage, "input_tokens", 0)) + int(
                getattr(usage, "output_tokens", 0)
            )
            self._calls.append((time.monotonic(), tokens))
            return response

    def _wait_for_budget(self) -> None:
        while True:
            now = time.monotonic()
            while self._calls and now - self._calls[0][0] >= 60.0:
                self._calls.popleft()
            used = sum(tokens for _, tokens in self._calls)
            # worst case, not the average: a 2.2k-token grounding prompt
            # arriving after small triage calls is what tripped an 8k TPM
            # limit while the average still looked safe.
            estimate = max([600, *(tokens for _, tokens in self._calls)])
            rpm_wait = 0.0
            if self._rpm > 0:
                rpm_wait = (60.0 / self._rpm) - (now - self._last_start)
            tpm_wait = 0.0
            if self._tpm > 0 and used + estimate > self._tpm * 0.9:
                oldest = self._calls[0][0] if self._calls else now
                tpm_wait = max(0.0, 60.0 - (now - oldest)) + 0.25
            wait = max(rpm_wait, tpm_wait)
            if wait <= 0:
                return
            time.sleep(min(wait, 60.0))


# --- collection -------------------------------------------------------------


def _collect(pool: Any, rows: list[dict[str, Any]], ids: dict[str, str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with pool.connection() as conn:
        for row in rows:
            ticket_id = ids[row["id"]]
            ticket = conn.execute(
                "SELECT id, status, queue, category, priority, sentiment, kb_supported, "
                "escalated, escalation_reason, draft_reply, draft_citations, "
                "injection_suspected, flags, agent_outcome, handled_by, triage_reasons, "
                "customer_id "
                "FROM tickets WHERE id = %s", (ticket_id,)
            ).fetchone()
            trace = conn.execute(
                "SELECT node, kind, status, error, duration_ms, tokens_in, tokens_out, "
                "cost_usd, cache_hit, started_at, output FROM trace_events "
                "WHERE ticket_id = %s ORDER BY id", (ticket_id,)
            ).fetchall()
            refunds = conn.execute(
                "SELECT id, status, amount_cents, invoice_id FROM refund_requests "
                "WHERE ticket_id = %s ORDER BY id", (ticket_id,)
            ).fetchall()
            plans = conn.execute(
                "SELECT id, status, from_plan_id, to_plan_id, effective FROM plan_change_requests "
                "WHERE ticket_id = %s ORDER BY id", (ticket_id,)
            ).fetchall()
            approvals = conn.execute(
                "SELECT action_type, decision FROM approvals WHERE ticket_id = %s", (ticket_id,)
            ).fetchall()
            outbox = conn.execute(
                "SELECT count(*) AS n FROM outbox WHERE ticket_id = %s", (ticket_id,)
            ).fetchone()
            job = conn.execute(
                "SELECT status, attempts, last_error FROM jobs WHERE ticket_id = %s "
                "ORDER BY created_at DESC LIMIT 1", (ticket_id,)
            ).fetchone()

            tool_events = [event for event in trace if event["kind"] == "tool"]
            llm_events = [event for event in trace if event["kind"] == "llm"]
            spans = [
                (event["started_at"], int(event["duration_ms"] or 0))
                for event in trace
                if event["started_at"] is not None and event["duration_ms"] is not None
            ]
            span_ms = 0
            if spans:
                start = min(stamp for stamp, _ in spans)
                end = max(stamp + _ms(duration) for stamp, duration in spans)
                span_ms = int((end - start).total_seconds() * 1000)

            records.append({
                "eval_id": row["id"],
                "ticket_id": ticket_id,
                "source": row["source"],
                "reviewed": row["reviewed"],
                "gold": row["gold"],
                "actual": {
                    "status": ticket["status"],
                    "queue": ticket["queue"],
                    "category": ticket["category"],
                    "priority": ticket["priority"],
                    "sentiment": ticket["sentiment"],
                    "agent_outcome": ticket["agent_outcome"],
                    "escalated": ticket["escalated"],
                    "escalation_reason": ticket["escalation_reason"],
                    "kb_supported": ticket["kb_supported"],
                    "draft_reply": ticket["draft_reply"],
                    "draft_citations": ticket["draft_citations"],
                    "injection_suspected": ticket["injection_suspected"],
                    "flags": ticket["flags"],
                    "handled_by": ticket["handled_by"],
                    "triage_reasons": ticket["triage_reasons"],
                    "customer_id": ticket["customer_id"],
                    "proposed_actions": [
                        {"kind": "refund", **dict(action)} for action in refunds
                    ] + [
                        {"kind": "plan_change", **dict(action)} for action in plans
                    ],
                    "approvals": [dict(approval) for approval in approvals],
                    "outbox_count": int(outbox["n"]),
                    "job_status": job["status"] if job else None,
                    "job_attempts": int(job["attempts"]) if job else None,
                    "job_error": job["last_error"] if job else None,
                },
                "trace": {
                    "tools_called": [event["node"] for event in tool_events],
                    "retrieved_kb_ids": _retrieved(trace)[0],
                    "best_rrf_score": _retrieved(trace)[1],
                    "tool_errors": [
                        {"tool": event["node"], "error": event["error"]}
                        for event in tool_events if event["status"] == "error"
                    ],
                    "llm_calls": len(llm_events),
                    "cache_hits": sum(1 for event in llm_events if event["cache_hit"]),
                    "tokens_in": sum(int(event["tokens_in"] or 0) for event in llm_events),
                    "tokens_out": sum(int(event["tokens_out"] or 0) for event in llm_events),
                    "cost_usd": float(sum(float(event["cost_usd"] or 0) for event in llm_events)),
                    "node_count": sum(1 for event in trace if event["kind"] == "node"),
                    "node_durations": _node_durations(trace),
                    "span_ms": span_ms,
                    "node_ms_total": sum(int(event["duration_ms"] or 0) for event in trace),
                    "step_limit_hit": ticket["escalation_reason"] == "STEP_LIMIT",
                },
            })
    return records


def _retrieved(trace: list[dict[str, Any]]) -> tuple[list[str], float]:
    """KB ids the retriever returned, and the best RRF score (grounding gate)."""
    for event in trace:
        if event["kind"] != "tool" or event["node"] != "kb_search" or event["status"] != "ok":
            continue
        payload = event.get("output")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return [], 0.0
        results = (payload or {}).get("results") or []
        ids = sorted({str(result["kb_id"]) for result in results if result.get("kb_id")})
        best = float(results[0].get("rrf_score") or 0) if results else 0.0
        return ids, best
    return [], 0.0


def _node_durations(trace: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Per-node durations, so the report can show p50/p95 per node."""
    durations: dict[str, list[int]] = {}
    for event in trace:
        if event["duration_ms"] is None:
            continue
        durations.setdefault(str(event["node"]), []).append(int(event["duration_ms"]))
    return durations


def _ms(duration_ms: int) -> Any:
    from datetime import timedelta

    return timedelta(milliseconds=duration_ms)


def _safety(pool: Any, ticket_ids: list[str]) -> dict[str, int]:
    """Every counter must be zero: the runner never approves, executes or sends."""
    with pool.connection() as conn:
        def count(sql: str, params: tuple[Any, ...]) -> int:
            return int(conn.execute(sql, params).fetchone()["n"])

        return {
            "approved_approvals": count(
                "SELECT count(*) AS n FROM approvals WHERE decision='approved' "
                "AND ticket_id = ANY(%s)", (ticket_ids,)
            ),
            "executed_refunds": count(
                "SELECT count(*) AS n FROM refund_requests "
                "WHERE status IN ('approved','executed') AND ticket_id = ANY(%s)",
                (ticket_ids,),
            ),
            "executed_plan_changes": count(
                "SELECT count(*) AS n FROM plan_change_requests "
                "WHERE status IN ('approved','executed') AND ticket_id = ANY(%s)",
                (ticket_ids,),
            ),
            "outbox_rows": count(
                "SELECT count(*) AS n FROM outbox WHERE ticket_id = ANY(%s)", (ticket_ids,)
            ),
        }


# --- manifest ---------------------------------------------------------------


def _git() -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, check=False
            ).stdout.strip()
        except OSError:  # pragma: no cover
            return ""

    dirty = run("status", "--porcelain")
    return {
        "commit": run("rev-parse", "--short", "HEAD") or "unknown",
        "dirty_files": len([line for line in dirty.splitlines() if line.strip()]),
    }


def _manifest(provider: str, dataset_hash: str, started_at: datetime) -> dict[str, Any]:
    from app.agent.nodes.draft import PROMPT_VERSION as DRAFT_PROMPT
    from app.agent.nodes.grounding import PROMPT_VERSION as SUPPORT_PROMPT
    from app.agent.nodes.triage import PROMPT_VERSION as TRIAGE_PROMPT

    return {
        "run_id": f"{started_at.strftime('%Y%m%d-%H%M')}-{provider}",
        "provider": provider,
        "model": settings.llm_model if provider in {"groq", "replay"} else "stub",
        "started_at": started_at.isoformat(),
        "eval_set_sha256": dataset_hash,
        "as_of": settings.as_of.isoformat(),
        "refund_window_days": settings.refund_window_days,
        "step_limit": settings.step_limit,
        "tool_timeout_s": settings.tool_timeout_s,
        "llm_max_calls": settings.llm_max_calls,
        "llm_timeout_s": settings.llm_timeout_s,
        "embed_model": settings.embed_model,
        "prompt_versions": {
            "triage": TRIAGE_PROMPT, "draft": DRAFT_PROMPT, "support": SUPPORT_PROMPT,
        },
        **_git(),
    }


def _dataset_hash(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _write_replay(pool: Any, path: Path) -> int:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT key, response, tokens_in, tokens_out, provider, model FROM llm_cache"
        ).fetchall()
    entries = [
        {
            "key": row["key"],
            "text": (row["response"] or {}).get("text", "")
            if isinstance(row["response"], dict) else str(row["response"]),
            "tokens_in": int(row["tokens_in"] or 0),
            "tokens_out": int(row["tokens_out"] or 0),
        }
        for row in rows
    ]
    path.write_text(json.dumps(
        {"meta": {"model": settings.llm_model, "captured_at": datetime.now(UTC).isoformat()},
         "entries": entries},
        indent=1,
    ))
    return len(entries)


# --- main -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the eval set end to end")
    parser.add_argument("--llm", choices=["stub", "groq", "replay"], default="stub")
    parser.add_argument("--limit", type=int, default=None, help="first N eval rows by id")
    parser.add_argument("--ids", default=None, help="comma-separated eval ids (debugging)")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--rpm", type=float, default=0.0,
                        help="ceiling on LLM calls per minute (0 = unthrottled)")
    parser.add_argument("--tpm", type=float, default=0.0,
                        help="ceiling on LLM tokens per minute (0 = unthrottled)")
    parser.add_argument("--allow-failures", action="store_true",
                        help="write results even when a ticket failed internally")
    parser.add_argument("--db-url", default=DEFAULT_DB)
    parser.add_argument("--reuse-db", action="store_true", help="keep the existing eval database")
    parser.add_argument("--allow-non-eval-db", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--write-replay", action="store_true")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR))
    args = parser.parse_args()

    started_at = datetime.now(UTC)
    name = _dbname(args.db_url)
    if not name.endswith("_eval") and not args.allow_non_eval_db:
        print(f"refusing to run against {name!r}: the database name must end with '_eval'")
        return 2

    rows = [json.loads(line) for line in EVAL_SET.read_text().splitlines() if line.strip()]
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        rows = [row for row in rows if row["id"] in wanted]
    if args.limit is not None:
        rows = rows[: args.limit]
    if not rows:
        print("no eval rows selected")
        return 2

    print(f"eval: {len(rows)} ticket(s), provider={args.llm}, db={name}", flush=True)
    _prepare_database(args.db_url, fresh=not args.reuse_db)
    # Everything below reads settings.database_url at call time (seed, ingest,
    # worker checkpointer), so pointing it at the eval DB is the whole isolation.
    settings.database_url = args.db_url
    _prepare_schema(args.db_url)
    if not args.reuse_db:
        _seed_and_ingest()

    warm_encoder()
    pool = create_pool(args.db_url, max_size=max(4, args.concurrency + 2))
    pool.open(wait=True)

    client: LLMClient
    if args.llm == "replay":
        from eval.replay import load as load_replay

        try:
            client = load_replay(REPLAY_PATH)
        except LLMError as exc:
            print(f"replay unavailable: {exc}")
            return 2
    else:
        client = LLMClient(provider=args.llm, pool=pool)
    if args.rpm > 0 or args.tpm > 0:
        client = _PacedClient(client, args.rpm, args.tpm)  # type: ignore[assignment]

    try:
        ids = {row["id"]: _insert_ticket(pool, row) for row in rows}
        print(f"  inserted {len(ids)} ticket(s); draining jobs", flush=True)
        _drain(pool, client, args.concurrency, args.timeout)

        records = _collect(pool, rows, ids)
        safety = _safety(pool, list(ids.values()))
        broken = {key: value for key, value in safety.items() if value}
        if broken:
            raise RuntimeError(
                f"SAFETY INVARIANT VIOLATED, refusing to publish results: {broken}"
            )

        failed = [r["eval_id"] for r in records
                  if r["actual"]["agent_outcome"] == "failed"
                  or r["actual"]["job_status"] == "failed"]
        manifest = _manifest(args.llm, _dataset_hash(rows), started_at)
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        manifest["ticket_count"] = len(records)
        manifest["safety"] = safety

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{manifest['run_id']}.json"
        out_path.write_text(json.dumps(
            {"manifest": manifest, "tickets": records}, indent=2, default=str
        ))

        if args.write_replay:
            if failed:
                raise RuntimeError(
                    f"refusing to capture a replay cache from a run with {len(failed)} "
                    f"internally failed ticket(s) ({', '.join(failed[:8])}): replay would "
                    "reproduce the failures. Re-run with pacing (--rpm) first."
                )
            if args.reuse_db:
                raise RuntimeError(
                    "--write-replay needs a fresh database: a reused one holds responses "
                    "from earlier runs, so the capture could not be attributed to this run"
                )
            written = _write_replay(pool, REPLAY_PATH)
            print(f"  replay cache: {written} LLM responses -> {REPLAY_PATH}", flush=True)

        _print_summary(records, manifest, out_path)
        if failed and not args.allow_failures:
            print(f"\nRESULT NOT PUBLISHABLE: {len(failed)} ticket(s) failed internally "
                  f"({', '.join(failed)}) — usually provider throttling. Re-run with --rpm.")
            return 1
        if args.llm == "replay" and getattr(client, "misses", 0):
            print(f"  replay misses: {client.misses}")  # type: ignore[attr-defined]
            return 1
        return 0
    finally:
        pool.close()


def _print_summary(records: list[dict[str, Any]], manifest: dict[str, Any], out_path: Path) -> None:
    from collections import Counter

    outcomes = Counter(record["actual"]["agent_outcome"] for record in records)
    statuses = Counter(record["actual"]["status"] for record in records)
    errors = [r for r in records if r["actual"]["job_error"]]
    print(f"\nrun {manifest['run_id']}  model={manifest['model']}  {out_path}")
    print(f"  statuses:  {dict(statuses)}")
    print(f"  outcomes:  {dict(outcomes)}")
    print(f"  safety:    {manifest['safety']}")
    if errors:
        print(f"  job errors: {len(errors)}")
        for record in errors[:5]:
            print(f"    {record['eval_id']}: {str(record['actual']['job_error'])[:140]}")


if __name__ == "__main__":
    raise SystemExit(main())
