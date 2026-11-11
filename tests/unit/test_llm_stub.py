"""Stub determinism: constrained schemas need stub_example() (PLAN Phase 2).

The naive annotation-based filler produces None/0/"stub" values that are
invalid for Literal fields, nested models, Field bounds, and min_length
strings. Convention: structured-output schemas define `stub_example()`; any
schema without one that the filler cannot satisfy must raise LLMError in CI
rather than silently pass bad data to the graph.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from app.llm.base import LLMClient, LLMError


class Citation(BaseModel):
    kb_id: str
    snippet: str = Field(min_length=5)


class TriageStubOutput(BaseModel):
    """Shaped like the future triage output: constrained types only."""

    outcome: Literal["draft_ready", "action_proposed", "escalated"]
    category: Literal["invoice_question", "refund_request", "other"]
    confidence: float = Field(ge=0.5, le=1.0)
    summary: str = Field(min_length=10)
    citations: list[Citation]

    @classmethod
    def stub_example(cls) -> dict[str, Any]:
        return {
            "outcome": "draft_ready",
            "category": "invoice_question",
            "confidence": 0.75,
            "summary": "Customer asks about invoice status.",
            "citations": [{"kb_id": "KB-001", "snippet": "Invoice lifecycle"}],
        }


class ConstrainedNoExample(BaseModel):
    mode: Literal["strict"]
    score: float = Field(ge=0.9)


def test_stub_example_is_used_and_valid() -> None:
    result = LLMClient("stub").generate(
        [{"role": "user", "content": "triage"}],
        schema=TriageStubOutput,
    )
    assert result.structured is not None
    assert result.structured.outcome == "draft_ready"
    assert result.structured.confidence >= 0.5
    assert result.structured.citations[0].kb_id == "KB-001"


def test_stub_without_example_fails_loudly_on_unsupported_shapes() -> None:
    with pytest.raises(LLMError, match="stub output failed schema validation"):
        LLMClient("stub").generate(
            [{"role": "user", "content": "classify"}],
            schema=ConstrainedNoExample,
        )


def test_stub_example_must_itself_be_schema_valid() -> None:
    class BadExample(BaseModel):
        mode: Literal["strict"]

        @classmethod
        def stub_example(cls) -> dict[str, Any]:
            return {"mode": "loose"}

    # generate() parses the stub text through parse_structured, so a bad
    # example surfaces as LLMError instead of a silently wrong value.
    with pytest.raises(LLMError):
        LLMClient("stub").generate([{"role": "user", "content": "x"}], schema=BadExample)
