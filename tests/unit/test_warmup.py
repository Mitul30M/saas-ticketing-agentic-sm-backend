"""The embedding warm-up must reuse the module-level encoder.

A cold model load used to exceed TOOL_TIMEOUT_S on the first kb_search of a
fresh deployment; the API lifespan and the worker now warm the same instance
the tools use. These tests keep that contract without loading torch.
"""

from __future__ import annotations

from typing import Any

from app.retrieval import hybrid


class _FakeEncoder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def encode(self, text: str, normalize_embeddings: bool = False) -> list[float]:
        self.calls.append((text, normalize_embeddings))
        return [0.1, 0.2, 0.3]


def test_warm_encoder_loads_and_encodes_once(monkeypatch: Any) -> None:
    fake = _FakeEncoder()
    monkeypatch.setattr(hybrid, "_encoder_instance", lambda: fake)

    seconds = hybrid.warm_encoder()

    assert seconds >= 0.0
    assert fake.calls == [("warmup", True)]


def test_embed_query_uses_the_same_instance(monkeypatch: Any) -> None:
    fake = _FakeEncoder()
    monkeypatch.setattr(hybrid, "_encoder_instance", lambda: fake)

    assert hybrid.embed_query("refund window") == [0.1, 0.2, 0.3]
    assert fake.calls == [("refund window", True)]
