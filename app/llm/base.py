"""Provider-independent LLM boundary.

Graph and tool code imports this module rather than a vendor SDK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from types import UnionType
from typing import Any, TypeVar, get_args, get_origin

from pydantic import BaseModel, SecretStr, ValidationError
from pydantic.fields import PydanticUndefined

from app.config import settings
from app.llm.cache import cache_key, load_cached, store_cached

SchemaT = TypeVar("SchemaT", bound=BaseModel)
Message = dict[str, str]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    structured: BaseModel | None
    usage: Usage
    cache_hit: bool = False


class LLMError(RuntimeError):
    """A safe, provider-independent LLM failure."""


def parse_structured[SchemaT: BaseModel](text: str, schema: type[SchemaT]) -> SchemaT:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError("LLM returned invalid JSON") from exc
    try:
        return schema.model_validate(value)
    except ValidationError as exc:
        # Name the first offending field: a bare "did not match" made a real
        # model failure undiagnosable and sent the repair prompt chasing JSON
        # syntax that was already fine.
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc") or ()) or "<root>"
        raise LLMError(
            f"LLM JSON did not match the requested schema ({where}: {first.get('msg')})"
        ) from exc


class LLMClient:
    """Small client facade with stub and Groq providers.

    Pass a psycopg_pool to enable the DB-backed LLM cache (identical requests
    are answered from cache, PLAN 4.6 layer 3). Without a pool, no caching.
    """

    def __init__(self, provider: str | None = None, pool: Any | None = None) -> None:
        self.provider = provider or settings.llm_provider
        self._pool = pool
        if self.provider not in {"stub", "groq"}:
            raise ValueError(f"unsupported LLM provider: {self.provider}")

    def generate(
        self,
        messages: list[Message],
        schema: type[SchemaT] | None = None,
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> LLMResponse:
        started = perf_counter()
        key = cache_key(self.provider, settings.llm_model or self.provider, messages, params)
        if self._pool is not None:
            with self._pool.connection() as conn:
                cached = load_cached(conn, key)
            if cached is not None:
                payload = cached["response"]
                text = payload["text"] if isinstance(payload, dict) else str(payload)
                structured = parse_structured(text, schema) if schema else None
                latency_ms = int((perf_counter() - started) * 1000)
                return LLMResponse(
                    text,
                    structured,
                    Usage(
                        input_tokens=cached["tokens_in"] or 0,
                        output_tokens=cached["tokens_out"] or 0,
                        latency_ms=latency_ms,
                    ),
                    cache_hit=True,
                )

        text, tokens_in, tokens_out = self._raw_generate(messages, schema, tools, params)
        repaired_text, repaired_structured = self._parse_with_repair(messages, schema, tools, text)
        text, structured = repaired_text, repaired_structured
        if self._pool is not None:
            with self._pool.connection() as conn:
                store_cached(
                conn, key, {"text": text}, tokens_in, tokens_out,
                provider=self.provider, model=settings.llm_model or self.provider,
            )
        latency_ms = int((perf_counter() - started) * 1000)
        return LLMResponse(text, structured, Usage(tokens_in, tokens_out, latency_ms))

    def _raw_generate(
        self,
        messages: list[Message],
        schema: type[SchemaT] | None,
        tools: list[dict[str, Any]] | None,
        params: dict[str, Any],
    ) -> tuple[str, int, int]:
        """Provider call. Returns (text, tokens_in, tokens_out)."""
        if self.provider == "stub":
            return self._stub_response(messages, schema), 0, 0
        return self._groq_response(messages, tools, params)

    def _parse_with_repair(
        self,
        messages: list[Message],
        schema: type[SchemaT] | None,
        tools: list[dict[str, Any]] | None,
        text: str,
    ) -> tuple[str, SchemaT | None]:
        """Parse schema output with ONE bounded repair: strip code fences, then
        a single re-ask for real providers. Stub is deterministic, so a failed
        stub self-validation must fail loudly, not be papered over."""
        if schema is None:
            return text, None
        if self.provider == "stub":
            return text, parse_structured(text, schema)  # stub text was self-validated
        try:
            return text, parse_structured(text, schema)
        except LLMError:
            pass
        repaired = _strip_fences(text)
        try:
            return repaired, parse_structured(repaired, schema)
        except LLMError:
            pass
        retry_messages = messages + [{
            "role": "user",
            "content": "Your previous reply did not parse as the required JSON schema. "
                       "Reply again with ONLY the JSON object, every field present, "
                       "no prose, no markdown fences.",
        }]
        retry_text, _, _ = self._raw_generate(retry_messages, schema, tools, {})
        final_text = _strip_fences(retry_text)
        return final_text, parse_structured(final_text, schema)  # 2nd failure -> LLMError

    @staticmethod
    def _stub_response(messages: list[Message], schema: type[SchemaT] | None) -> str:
        if schema is None:
            return "Stub response."
        # Preferred: the schema supplies a realistic example (deterministic,
        # valid even for Literals, nested models, and Field constraints).
        example = getattr(schema, "stub_example", None)
        if callable(example):
            return json.dumps(example())
        # Fallback: fill fields from annotations, then SELF-VALIDATE. Any
        # shape the naive filler cannot satisfy fails loudly here instead of
        # silently pushing bad data through the graph.
        values = {
            name: _stub_value(field.annotation, field.default)
            for name, field in schema.model_fields.items()
        }
        text = json.dumps(values)
        try:
            schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMError(
                "stub output failed schema validation; define stub_example() on the schema"
            ) from exc
        return text

    @staticmethod
    def _groq_response(
        messages: list[Message], tools: list[dict[str, Any]] | None, params: dict[str, Any]
    ) -> tuple[str, int, int]:
        try:
            from langchain_groq import ChatGroq
        except ImportError as exc:
            raise LLMError("Groq provider is not installed") from exc
        try:
            model = ChatGroq(
                api_key=SecretStr(settings.llm_api_key),
                model=settings.llm_model,
                temperature=params.pop("temperature", 0),
                timeout=settings.llm_timeout_s,
                max_retries=settings.llm_max_calls - 1,
            )
            response = model.invoke(messages)
        except Exception as exc:
            # keep the provider's own words: "failed" alone made a rate-limit
            # 429 indistinguishable from an auth or model-id error
            detail = f"{type(exc).__name__}: {exc}"[:300]
            raise LLMError(f"Groq request failed ({detail})") from exc
        content = response.content
        if not isinstance(content, str):
            raise LLMError("Groq returned a non-text response")
        usage = getattr(response, "usage_metadata", None) or {}
        return content, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _strip_fences(text: str) -> str:
    """Repair step 1: models love wrapping JSON in ``` fences."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().endswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _stub_value(annotation: Any, default: Any) -> Any:
    if default is not PydanticUndefined:
        return default
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (UnionType,):
        non_null = [item for item in args if item is not type(None)]
        return _stub_value(non_null[0], PydanticUndefined) if non_null else None
    if origin is list:
        return []
    if origin is dict:
        return {}
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return next(iter(annotation))
    if annotation is str:
        return "stub"
    if annotation is int or annotation is float:
        return 0
    if annotation is bool:
        return False
    return None
