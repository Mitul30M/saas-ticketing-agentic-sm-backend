"""Single-process job worker; deployment may run multiple copies safely."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from langgraph.types import Command

from app.agent.checkpointer import make_checkpointer
from app.agent.graph import build_graph
from app.config import settings
from app.db import create_pool
from app.services import jobs
from app.services.executor import execute_approved_reply
from app.services.ticket_state import apply_run_result


def run_one(pool: Any, worker_id: str | None = None) -> bool:
    job = jobs.claim(pool, worker_id or f"worker-{os.getpid()}")
    if job is None:
        return False
    ticket_id = str(job["ticket_id"])
    saver = make_checkpointer(settings.database_url)
    try:
        graph = build_graph(pool, checkpointer=saver)
        config = {
            "configurable": {"thread_id": f"ticket:{ticket_id}"},
            "recursion_limit": max(settings.step_limit * 2 + 4, 25),
        }
        payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
        if job["kind"] == "resume_agent":
            result = graph.invoke(Command(resume=payload), config=config)
            apply_run_result(pool, ticket_id, result)
            if payload.get("approved"):
                execute_approved_reply(
                    pool, ticket_id=ticket_id,
                    approval_id=str(payload["approval_id"]),
                    approved_by=str(payload["decided_by"]),
                )
                with pool.connection() as conn:
                    conn.execute(
                        "UPDATE tickets SET status='resolved', agent_outcome='action_proposed', "
                        "handled_by='agent' WHERE id=%s", (ticket_id,)
                    )
        else:
            with pool.connection() as conn:
                conn.execute("UPDATE tickets SET status='triaging' WHERE id=%s", (ticket_id,))
            result = graph.invoke({"ticket_id": ticket_id}, config=config)
            apply_run_result(pool, ticket_id, result)
        jobs.complete(pool, str(job["id"]))
    except Exception as exc:
        jobs.fail(pool, job, str(exc))
    finally:
        saver.conn.close()
    return True


def run_loop(pool: Any) -> None:
    while True:
        if not run_one(pool):
            time.sleep(1)


if __name__ == "__main__":
    pool = create_pool(settings.database_url)
    pool.open(wait=True)
    try:
        run_loop(pool)
    finally:
        pool.close()
