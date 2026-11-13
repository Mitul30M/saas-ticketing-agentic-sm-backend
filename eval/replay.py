"""Deterministic replay of a captured real-model run.

`make eval` must reproduce the published table on a machine without an API key,
so a `--llm groq` run can export its LLM cache and a later `--llm replay` run
serves exactly those responses. A missed key is a hard error, never a silent
call to a provider: a replay that quietly went live would not be reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.llm.base import LLMClient, LLMError, Message
from app.llm.cache import cache_key

DEFAULT_PATH = Path("eval/replay_cache.json")


def _key(messages: list[Message], params: dict[str, Any]) -> str:
    return cache_key("groq", settings.llm_model or "groq", messages, params)


class ReplayLLM(LLMClient):
    """LLMClient that answers from a captured cache (provider keys must match)."""

    def __init__(self, entries: dict[str, dict[str, Any]], model: str) -> None:
        # provider stays "groq" so cache keys are identical to the capture run;
        # pool=None so the DB cache cannot mask a miss.
        super().__init__(provider="groq", pool=None)
        self._entries = entries
        self._model = model
        self.misses = 0

    def _raw_generate(
        self,
        messages: list[Message],
        schema: Any,
        tools: list[dict[str, Any]] | None,
        params: dict[str, Any],
    ) -> tuple[str, int, int]:
        key = _key(messages, params)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            raise LLMError(
                f"replay cache miss ({key[:12]}); this prompt was not part of the captured run"
            )
        return str(entry["text"]), int(entry.get("tokens_in") or 0), int(entry.get("tokens_out") or 0)


def load(path: Path = DEFAULT_PATH) -> ReplayLLM:
    if not path.exists():
        raise LLMError(
            f"{path} is missing: run `python -m eval.run_eval --llm groq --write-replay` first"
        )
    payload = json.loads(path.read_text())
    meta = payload.get("meta") or {}
    model = str(meta.get("model") or "")
    if model and settings.llm_model and model != settings.llm_model:
        raise LLMError(
            f"replay was captured with model {model!r} but LLM_MODEL is "
            f"{settings.llm_model!r}; cache keys would not match"
        )
    entries = {str(row["key"]): row for row in payload.get("entries") or []}
    if not entries:
        raise LLMError(f"{path} has no cache entries")
    return ReplayLLM(entries, model or settings.llm_model)
