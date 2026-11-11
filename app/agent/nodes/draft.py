"""Draft node: one structured LLM call drafts the reply citing retrieved
chunks, then a DETERMINISTIC citation validator runs — every [KB-xxx] in the
draft and every claimed citation must be inside the retrieved set. Invented
citations -> reject the draft and escalate."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.nodes.common import escalation_update, next_step, step_guard, trace_node
from app.agent.nodes.triage import BODY_CLOSE, BODY_OPEN, SUBJECT_CLOSE, SUBJECT_OPEN
from app.agent.resilience import call_llm
from app.agent.state import AgentState
from app.llm.base import LLMClient

PROMPT_VERSION = "draft_v1"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_VERSION}.txt"

CITATION_RE = re.compile(r"\[(KB-[A-Za-z0-9_-]+)\]")


class DraftOutput(BaseModel):
    reply: str = Field(min_length=1, max_length=4000)
    citations: list[str] = Field(default_factory=list)

    @classmethod
    def stub_example(cls) -> dict[str, Any]:
        return {
            "reply": "Stub response: here is what the knowledge base says about "
                     "your question.",
            "citations": [],
        }


@lru_cache(maxsize=4)
def _system_prompt(version: str = PROMPT_VERSION) -> str:
    prompt = _PROMPT_FILE.read_text(encoding="utf-8").strip()
    if SUBJECT_OPEN in prompt or BODY_OPEN in prompt:
        raise RuntimeError("system prompt must not contain ticket delimiters")
    return prompt


def _user_content(subject: str, body: str, chunks: list[dict[str, Any]]) -> str:
    parts = [
        f"{SUBJECT_OPEN}\n{subject}\n{SUBJECT_CLOSE}",
        f"{BODY_OPEN}\n{body}\n{BODY_CLOSE}",
        "<<<RETRIEVED_CHUNKS",
    ]
    for index, chunk in enumerate(chunks, start=1):
        parts.append(
            f"[{index}] kb_id={chunk['kb_id']} chunk={chunk['chunk_idx']} "
            f"title={chunk['title']}\n{chunk['text']}"
        )
    parts.append("RETRIEVED_CHUNKS>>>")
    return "\n\n".join(parts)


def validate_citations(reply: str, claimed: list[str], retrieved_ids: set[str]) -> list[str]:
    """Deterministic gate: inline [KB-xxx] citations + the claimed list must all
    be inside the retrieved set. Returns the sorted union of valid citations."""
    inline = set(CITATION_RE.findall(reply))
    claimed_set = {str(c) for c in claimed}
    if not inline <= retrieved_ids or not claimed_set <= retrieved_ids:
        raise CitationError()
    return sorted(inline | claimed_set)


class CitationError(RuntimeError):
    """Raised when a draft cites knowledge that was never retrieved."""


def make_draft_node(pool: Any, client: LLMClient):
    def draft(state: AgentState) -> dict[str, Any]:
        guard = step_guard(state)
        if guard is not None:
            return guard
        ticket_id = str(state["ticket_id"])
        trace_id = str(state["trace_id"])
        chunks = state.get("retrieved") or []
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _user_content(state["subject"], state["body"], chunks)},
        ]
        response = call_llm(
            lambda: client.generate(messages, schema=DraftOutput),
            pool=pool, ticket_id=ticket_id, trace_id=trace_id, node="draft_reply",
        )
        output = response.structured
        if not isinstance(output, DraftOutput):
            raise TypeError("draft produced no structured output")

        try:
            citations = validate_citations(
                output.reply, output.citations, {str(c["kb_id"]) for c in chunks}
            )
        except CitationError:
            trace_node(
                pool, ticket_id, trace_id, "draft_reply", "decision",
                {"prompt_version": PROMPT_VERSION},
                {"accepted": False, "reason_code": "DRAFT_CITATION_INVALID"},
            )
            return {
                **escalation_update(
                    "DRAFT_CITATION_INVALID",
                    "draft cited knowledge outside the retrieved set; rejected",
                ),
                "step_count": next_step(state),
            }

        trace_node(
            pool, ticket_id, trace_id, "draft_reply", "llm",
            {"prompt_version": PROMPT_VERSION},
            {"accepted": True, "citations": citations},
            duration_ms=response.usage.latency_ms,
            tokens_in=response.usage.input_tokens, tokens_out=response.usage.output_tokens,
            cache_hit=response.cache_hit,
        )
        return {
            "draft": output.reply,
            "citations": citations,
            "step_count": next_step(state),
        }

    return draft
