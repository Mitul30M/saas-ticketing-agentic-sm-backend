"""CLI runner for the agent (PLAN Phase 3 DoD).

    python -m app.agent.run --ticket <uuid>              # fresh run
    python -m app.agent.run --ticket <uuid> --resume-approved --decided-by mia
    python -m app.agent.run --ticket <uuid> --resume-rejected --decided-by mia

Prints the final state summary and the trace (with token totals) so the run
can be verified without a UI."""

from __future__ import annotations

import argparse
import json
from typing import Any

from langgraph.types import Command

from app import db
from app.agent.checkpointer import make_checkpointer
from app.agent.graph import build_graph
from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the triage agent on one ticket")
    parser.add_argument("--ticket", required=True, help="tickets.id (uuid)")
    parser.add_argument("--resume-approved", action="store_true",
                        help="resume a paused run, approving the draft")
    parser.add_argument("--resume-rejected", action="store_true",
                        help="resume a paused run, rejecting the draft")
    parser.add_argument("--decided-by", default="ops-console",
                        help="who made the decision (recorded in the audit)")
    parser.add_argument("--edited-text", default=None,
                        help="human-edited replacement draft")
    args = parser.parse_args()

    pool = db.create_pool(settings.database_url)
    pool.open()
    graph = build_graph(pool, checkpointer=make_checkpointer(settings.database_url))
    config: dict[str, Any] = {
        "configurable": {"thread_id": f"ticket:{args.ticket}"},
        "recursion_limit": max(settings.step_limit * 2 + 4, 25),
    }

    if args.resume_approved or args.resume_rejected:
        resume = {
            "approved": args.resume_approved and not args.resume_rejected,
            "decided_by": args.decided_by,
            "edited_text": args.edited_text,
        }
        result = graph.invoke(Command(resume=resume), config=config)
    else:
        result = graph.invoke({"ticket_id": args.ticket}, config=config)

    summary = {
        key: result.get(key)
        for key in ("ticket_id", "outcome", "triage", "citations", "escalation",
                    "approval", "flags", "step_count", "errors")
    }
    if result.get("__interrupt__"):
        summary["paused"] = "awaiting human approval (resume with --resume-approved/--resume-rejected)"
        summary["approval_request"] = json.loads(
            json.dumps(result["__interrupt__"][0].value, default=str)
        )
    summary["draft"] = (result.get("draft") or "")[:2000] or None
    print("=== FINAL STATE ===")
    print(json.dumps(summary, indent=2, default=str))

    print("=== TRACE ===")
    tokens_in = tokens_out = 0
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT node, kind, status, error, duration_ms, tokens_in, tokens_out, "
            "cache_hit FROM trace_events WHERE ticket_id::text = %s ORDER BY id",
            (args.ticket,),
        ).fetchall()
    for row in rows:
        tokens_in += row["tokens_in"] or 0
        tokens_out += row["tokens_out"] or 0
        status = row["status"] + (f" ({row['error']})" if row["error"] else "")
        meta = f" dur={row['duration_ms']}ms" if row["duration_ms"] is not None else ""
        cache = " cache_hit" if row["cache_hit"] else ""
        tokens = (f" tok={row['tokens_in']}/{row['tokens_out']}"
                  if row["tokens_in"] or row["tokens_out"] else "")
        print(f"{row['node']:<18} {row['kind']:<9} {status:<24}{meta}{tokens}{cache}")
    print(f"TOTAL tokens: in={tokens_in} out={tokens_out}")


if __name__ == "__main__":
    main()
