"""Triage node: the LLM proposes {category, priority, sentiment}; deterministic
code maps category -> queue (CATEGORY_QUEUE in app/domain/enums.py — never in
the prompt). The prompt file is versioned; ticket text is untrusted data,
always delimited, never part of the system prompt (invariant 5)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import CATEGORY_QUEUE, Category, Priority, Queue, Sentiment
from app.llm.base import LLMClient

PROMPT_VERSION = "triage_v1"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_VERSION}.txt"

SUBJECT_OPEN, SUBJECT_CLOSE = "<<<SUBJECT", "SUBJECT>>>"
BODY_OPEN, BODY_CLOSE = "<<<BODY", "BODY>>>"


class TriageReasons(BaseModel):
    category: str = Field(min_length=1, max_length=300)
    priority: str = Field(min_length=1, max_length=300)
    sentiment: str = Field(min_length=1, max_length=300)


class TriageOutput(BaseModel):
    """Structured triage verdict. Enums are fixed in app/domain/enums.py and
    reused by prompts, schemas, eval, and UI."""

    category: Category
    priority: Priority
    sentiment: Sentiment
    reasons: TriageReasons

    @classmethod
    def stub_example(cls) -> dict[str, Any]:
        return {
            "category": "invoice_question",
            "priority": "medium",
            "sentiment": "neutral",
            "reasons": {
                "category": "The customer asks about a charge on their invoice.",
                "priority": "It is a routine billing question.",
                "sentiment": "The tone is polite and neutral.",
            },
        }


@dataclass(frozen=True)
class TriageResult:
    category: Category
    priority: Priority
    sentiment: Sentiment
    queue: Queue  # deterministic code, never the LLM's decision
    reasons: dict[str, str]
    prompt_version: str
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    latency_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "priority": self.priority.value,
            "sentiment": self.sentiment.value,
            "queue": self.queue.value,
            "reasons": self.reasons,
            "prompt_version": self.prompt_version,
            "cache_hit": self.cache_hit,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "latency_ms": self.latency_ms,
            },
        }


@lru_cache(maxsize=4)
def _system_prompt(version: str = PROMPT_VERSION) -> str:
    path = _PROMPT_FILE if version == PROMPT_VERSION else _PROMPT_FILE.with_name(f"{version}.txt")
    prompt = path.read_text(encoding="utf-8").strip()
    # Invariant 5 guard: the system prompt is static and must never contain
    # ticket delimiters (ticket text only ever enters the user message).
    if SUBJECT_OPEN in prompt or BODY_OPEN in prompt:
        raise RuntimeError("system prompt must not contain ticket delimiters")
    return prompt


def _user_content(subject: str, body: str) -> str:
    return (
        f"{SUBJECT_OPEN}\n{subject}\n{SUBJECT_CLOSE}\n"
        f"{BODY_OPEN}\n{body}\n{BODY_CLOSE}"
    )


def triage_ticket(client: LLMClient, subject: str, body: str) -> TriageResult:
    """One structured LLM call; queue is mapped in code from the category."""
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _user_content(subject, body)},
    ]
    response = client.generate(messages, schema=TriageOutput)
    verdict = response.structured
    if not isinstance(verdict, TriageOutput):
        raise TypeError("triage schema produced no structured output")
    return TriageResult(
        category=verdict.category,
        priority=verdict.priority,
        sentiment=verdict.sentiment,
        queue=CATEGORY_QUEUE[verdict.category],
        reasons={
            "category": verdict.reasons.category,
            "priority": verdict.reasons.priority,
            "sentiment": verdict.reasons.sentiment,
        },
        prompt_version=PROMPT_VERSION,
        cache_hit=response.cache_hit,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=response.usage.latency_ms,
    )
