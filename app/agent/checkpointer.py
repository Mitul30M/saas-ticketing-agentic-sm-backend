"""PostgresSaver checkpointer on the SAME database (PLAN 4.6): a paused run
survives a process restart and resumes with Command(resume=...)."""

from __future__ import annotations

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver


def make_checkpointer(database_url: str) -> PostgresSaver:
    conn = psycopg.connect(database_url, autocommit=True, prepare_threshold=0)
    saver = PostgresSaver(conn)  # type: ignore[arg-type]  # stubs want a dict-row conn
    saver.setup()  # idempotent DDL for checkpoint tables
    return saver
