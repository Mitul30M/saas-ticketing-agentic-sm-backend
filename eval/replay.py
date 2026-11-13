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
from app.llm.base import LLMClient, LLMError, LLMResponse, Message, Usage, parse_structured
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

    def generate(  # type: ignore[override]
        self,
        messages: list[Message],
        schema: Any = None,
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> LLMResponse:
        """Answer from the capture, bypassing the base class's provider path.

        The base `generate` would call `_raw_generate` and then re-parse; the
        stored text is already the final (repaired) output, so this is both
        simpler and exact. `cache_hit=True` is honest: every replay answer comes
        from the capture, which is why the report marks the hit-rate metric as
        not meaningful for a replay run.
        """
        key = _key(messages, params)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            raise LLMError(
                f"replay cache miss ({key[:12]}); this prompt was not part of the captured run"
            )
        text = str(entry["text"])
        structured = parse_structured(text, schema) if schema is not None else None
        return LLMResponse(
            text,
            structured,
            Usage(
                input_tokens=int(entry.get("tokens_in") or 0),
                output_tokens=int(entry.get("tokens_out") or 0),
                latency_ms=0,
            ),
            cache_hit=True,
        )


def _current_prompt_versions() -> dict[str, str]:
    from app.agent.nodes.draft import PROMPT_VERSION as DRAFT
    from app.agent.nodes.grounding import PROMPT_VERSION as SUPPORT
    from app.agent.nodes.triage import PROMPT_VERSION as TRIAGE

    return {"triage": TRIAGE, "draft": DRAFT, "support": SUPPORT}


def load(path: Path = DEFAULT_PATH) -> ReplayLLM:
    if not path.exists():
        raise LLMError(
            f"{path} is missing: run `python -m eval.run_eval --llm groq --write-replay` first"
        )
    payload = json.loads(path.read_text())
    meta = payload.get("meta") or {}
    model = str(meta.get("model") or "")
    if model and settings.llm_model and model != settings.llm_model:
        # The cache is the source of truth for a replay: keys embed the model,
        # so serving a capture needs the capture's model regardless of .env.
        # Override with a visible note rather than failing a reproducible run.
        print(
            f"replay: using the captured model {model!r} "
            f"(LLM_MODEL was {settings.llm_model!r})"
        )
        settings.llm_model = model
    captured = meta.get("prompt_versions") or {}
    current = _current_prompt_versions()
    moved = {
        name: (captured.get(name), version)
        for name, version in current.items()
        if captured.get(name) and captured[name] != version
    }
    if moved:
        raising = ", ".join(f"{name} {before} -> {after}" for name, (before, after) in moved.items())
        raise LLMError(
            f"{path} was captured with different prompts ({raising}); replaying it would "
            "miss every cache key. Run `make eval-live` to capture the current prompts, or "
            f"check out the commit in {path} (meta.commit) to reproduce the old run."
        )

    entries = {str(row["key"]): row for row in payload.get("entries") or []}
    if not entries:
        raise LLMError(f"{path} has no cache entries")
    return ReplayLLM(entries, model or settings.llm_model)
