"""Single-process job worker; deployment may run multiple copies safely."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from langgraph.types import Command

from app.agent.checkpointer import make_checkpointer
from app.agent.graph import build_graph
from app.config import settings
from app.db import create_pool
from app.llm.base import LLMClient
from app.retrieval.hybrid import warm_encoder
from app.services import jobs
from app.services.executor import apply_due_plan_changes, execute_approved_reply
from app.services.ticket_state import apply_run_result, mark_resolved

logger = logging.getLogger(__name__)


def run_one(
    pool: Any, worker_id: str | None = None, client: LLMClient | None = None
) -> bool:
    job = jobs.claim(pool, worker_id or f"worker-{os.getpid()}")
    if job is None:
        return False
    ticket_id = str(job["ticket_id"])
    saver = make_checkpointer(settings.database_url)
    try:
        graph = build_graph(pool, client=client, checkpointer=saver)
        payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
        with pool.connection() as conn:
            ticket = conn.execute(
                "SELECT thread_id FROM tickets WHERE id=%s", (ticket_id,)
            ).fetchone()
        thread_id = str(ticket["thread_id"]) if ticket and ticket["thread_id"] else f"ticket:{ticket_id}"
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(settings.step_limit * 2 + 4, 25),
        }
        if job["kind"] == "resume_agent":
            state = graph.get_state(config)
            if getattr(state, "next", ()):
                result = graph.invoke(Command(resume=payload), config=config)
                result["thread_id"] = thread_id
                apply_run_result(pool, ticket_id, result)
            if payload.get("approved"):
                execute_approved_reply(
                    pool, ticket_id=ticket_id,
                    approval_id=str(payload["approval_id"]),
                    approved_by=str(payload["decided_by"]),
                )
                mark_resolved(pool, ticket_id)
        else:
            with pool.connection() as conn:
                conn.execute(
                    "UPDATE tickets SET status='triaging' WHERE id=%s "
                    "AND status IN ('new','awaiting_human')", (ticket_id,)
                )
            result = graph.invoke({"ticket_id": ticket_id}, config=config)
            result["thread_id"] = thread_id
            apply_run_result(pool, ticket_id, result)
        jobs.complete(pool, str(job["id"]))
    except Exception as exc:
        logger.exception("agent job failed", extra={"ticket_id": ticket_id})
        jobs.fail(pool, job, str(exc))
    finally:
        saver.conn.close()
    return True


def run_loop(pool: Any) -> None:
    last_cleanup = 0.0
    while True:
        if time.monotonic() - last_cleanup >= 300:
            from app.services.idempotency import cleanup

            try:
                cleanup(pool)
                apply_due_plan_changes(pool)
            except Exception:  # keep the worker alive; the error is logged
                logger.exception("periodic maintenance failed")
            last_cleanup = time.monotonic()
        if not run_one(pool):
            time.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    pool = create_pool(settings.database_url, application_name="nimbus-worker")
    pool.open(wait=True)
    # Warm once per process: the first kb_search of a fresh deployment used to
    # exceed TOOL_TIMEOUT_S while torch/the model loaded, escalating the ticket.
    try:
        seconds = warm_encoder()
        logger.info("embedding model warm in %.2fs", seconds)
    except Exception:
        logger.warning("embedding model warmup failed; first kb_search may time out")
    try:
        run_loop(pool)
    finally:
        pool.close()
