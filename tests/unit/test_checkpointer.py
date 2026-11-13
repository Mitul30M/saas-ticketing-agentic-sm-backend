"""The eval runner must not race the checkpointer's one-time DDL."""

from __future__ import annotations

import threading
from typing import Any

from app.agent import checkpointer


class _FakeConnection:
    pass


class _FakeSaver:
    setup_calls = 0

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def setup(self) -> None:
        type(self).setup_calls += 1


def test_setup_runs_once_per_database_under_concurrency(monkeypatch: Any) -> None:
    """Concurrent jobs on a fresh DB used to collide on checkpoint_migrations."""
    monkeypatch.setattr(checkpointer.psycopg, "connect", lambda *a, **k: _FakeConnection())
    monkeypatch.setattr(checkpointer, "PostgresSaver", _FakeSaver)
    monkeypatch.setattr(checkpointer, "_SETUP_DONE", set())
    _FakeSaver.setup_calls = 0

    threads = [
        threading.Thread(target=checkpointer.make_checkpointer, args=("postgresql://x/nimbus_eval",))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert _FakeSaver.setup_calls == 1


def test_a_different_database_is_set_up_separately(monkeypatch: Any) -> None:
    monkeypatch.setattr(checkpointer.psycopg, "connect", lambda *a, **k: _FakeConnection())
    monkeypatch.setattr(checkpointer, "PostgresSaver", _FakeSaver)
    monkeypatch.setattr(checkpointer, "_SETUP_DONE", set())
    _FakeSaver.setup_calls = 0

    checkpointer.make_checkpointer("postgresql://x/one_eval")
    checkpointer.make_checkpointer("postgresql://x/one_eval")
    checkpointer.make_checkpointer("postgresql://x/two_eval")

    assert _FakeSaver.setup_calls == 2
