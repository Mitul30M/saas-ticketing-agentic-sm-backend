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
        raise LLMError("LLM JSON did not match the requested schema") from exc


class LLMClient:
    """Small client facade with stub and Groq providers."""

    def __init__(self, provider: str | None = None) -> None:
        self.provider = provider or settings.llm_provider
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
        if self.provider == "stub":
            text = self._stub_response(messages, schema)
        else:
            text = self._groq_response(messages, tools, params)
        structured = parse_structured(text, schema) if schema else None
        latency_ms = int((perf_counter() - started) * 1000)
        return LLMResponse(text, structured, Usage(latency_ms=latency_ms))

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
    ) -> str:
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
            raise LLMError("Groq request failed") from exc
        content = response.content
        if not isinstance(content, str):
            raise LLMError("Groq returned a non-text response")
        return content


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
