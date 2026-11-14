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
from app.agent.record import render_account_record
from app.agent.resilience import call_llm
from app.agent.state import AgentState
from app.llm.base import LLMClient

PROMPT_VERSION = "draft_v6"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_VERSION}.txt"

CITATION_RE = re.compile(r"\[(KB-[A-Za-z0-9_-]+)\]")

MAX_REPLY_CHARS = 4000

# Deterministic promise-denial: a draft may never claim a refund, plan change
# or account action has been, is being, or will be carried out — those need a
# recorded human approval (AGENTS invariant 1). Prompt-only rules are not
# enough; the check below rejects such drafts before the approval gate sees
# them. Defense in depth, NOT the control: the human approval gate is the
# control (docs/DECISIONS.md).
PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"refund\b[^.?!]*\b(has been|will be|is being)\b", re.IGNORECASE),
    re.compile(r"\b(your|the)\s+refund\s+(is|has been)\s+(approved|processed|issued|on its way)", re.IGNORECASE),
    re.compile(r"\b(i|we)( have|'ve)?\s+(issued|processed|sent|approved)\s+(your|the|a)\s+refund", re.IGNORECASE),
    # past-tense claim that the refund already happened ("I have refunded $20")
    re.compile(r"\b(i|we)( have|'ve)?\s+refunded\b", re.IGNORECASE),
    re.compile(r"\b(plan|subscription|account)\b[^.?!]*\b(has been|will be|is being)\s+(changed|upgraded|downgraded|cancelled|canceled|switched)", re.IGNORECASE),
    re.compile(r"\b(i|we)( have|'ve| will|'ll)?\s+(upgraded|downgraded|changed|cancelled|canceled|switched)\s+your\s+(plan|subscription|account)", re.IGNORECASE),
    # future-tense commitments by the agent
    re.compile(r"\b(we(?:'ll| will)|i(?:'ll| will)|going to)\s+(process|issue|send|initiate|credit|return|refund)\b", re.IGNORECASE),
    re.compile(r"\byou(?:'ll| will)\s+(receive|get)\b[^.?!]*\brefund", re.IGNORECASE),
    re.compile(r"\b(?:initiated|started|submitted|scheduled)\s+(?:a|your|the|full|partial)\s+refund", re.IGNORECASE),
)


def rejects_promise_language(reply: str) -> bool:
    return any(pattern.search(reply) for pattern in PROMISE_PATTERNS)


class DraftOutput(BaseModel):
    reply: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)
    citations: list[str] = Field(default_factory=list)

    @classmethod
    def stub_example(cls) -> dict[str, Any]:
        return {
            "reply": "Stub response: here is what the knowledge base says [KB-006].",
            "citations": ["KB-006"],
        }


@lru_cache(maxsize=4)
def _system_prompt(version: str = PROMPT_VERSION) -> str:
    prompt = _PROMPT_FILE.read_text(encoding="utf-8").strip()
    if SUBJECT_OPEN in prompt or BODY_OPEN in prompt:
        raise RuntimeError("system prompt must not contain ticket delimiters")
    return prompt


def _user_content(
    subject: str, body: str, chunks: list[dict[str, Any]],
    record: dict[str, Any] | None = None,
) -> str:
    parts = [
        f"{SUBJECT_OPEN}\n{subject}\n{SUBJECT_CLOSE}",
        f"{BODY_OPEN}\n{body}\n{BODY_CLOSE}",
    ]
    if record:
        parts.append(render_account_record(record))
    parts.append("<<<RETRIEVED_CHUNKS")
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
            {"role": "user", "content": _user_content(
                state["subject"], state["body"], chunks, state.get("account_record")
            )},
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
            if not citations:
                raise CitationError()
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
        if rejects_promise_language(output.reply):
            # the model tried to promise a refund/plan/account action: those
            # need a recorded human approval, so the draft never reaches the gate
            trace_node(
                pool, ticket_id, trace_id, "draft_reply", "decision",
                {"prompt_version": PROMPT_VERSION},
                {"accepted": False, "reason_code": "DRAFT_PROMISE_DENIED"},
            )
            return {
                **escalation_update(
                    "DRAFT_REJECTED",
                    "draft promised a refund, plan change or account action; rejected",
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
        # Persist the approved-for-gate draft so the dashboard and the approval
        # API can show exactly what the human decided on, without reading the
        # LangGraph checkpoint. The executor later sends either this text or
        # the approval's edited_text — never a caller-supplied body.
        with pool.connection() as conn:
            conn.execute(
                "UPDATE tickets SET draft_reply = %s WHERE id = %s",
                (output.reply, ticket_id),
            )
        return {
            "draft": output.reply,
            "citations": citations,
            "step_count": next_step(state),
        }

    return draft
