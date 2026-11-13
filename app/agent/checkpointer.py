"""PostgresSaver checkpointer on the SAME database (PLAN 4.6): a paused run
survives a process restart and resumes with Command(resume=...)."""

from __future__ import annotations

import threading

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver

# `setup()` runs DDL plus a migration-row insert, which is not atomic against
# another process/thread doing the same thing. Two concurrent agent jobs on a
# fresh database raced and one job died with a unique violation on
# `checkpoint_migrations` (found by the eval runner at --concurrency 2, then
# retried by the job queue). Serialise it per database in this process; a
# multi-process first boot still relies on the job retry, which is why jobs have
# attempts. After the first success the tables exist and later calls are no-ops.
_SETUP_LOCK = threading.Lock()
_SETUP_DONE: set[str] = set()


def make_checkpointer(database_url: str) -> PostgresSaver:
    conn = psycopg.connect(database_url, autocommit=True, prepare_threshold=0)
    saver = PostgresSaver(conn)  # type: ignore[arg-type]  # stubs want a dict-row conn
    with _SETUP_LOCK:
        if database_url not in _SETUP_DONE:
            saver.setup()  # idempotent DDL for checkpoint tables
            _SETUP_DONE.add(database_url)
    return saver
