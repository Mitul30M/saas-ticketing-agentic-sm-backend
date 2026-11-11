"""Grounding check node (PLAN 4.5): three deterministic gates + one LLM check.

1. Retrieval gate: best RRF fused score must reach settings.grounding_threshold.
2. Support check: one structured LLM call maps each would-be claim to a
   supporting chunk; the LLM's kb_ids are VALIDATED against the retrieved set
   in code (a claimed citation that is not in the set is unsupported).
3. Conflict check: contradicting chunks -> escalate.

Every gate failure escalates with a reason code; gates never loosen permissions."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.nodes.common import escalation_update, next_step, step_guard, trace_node
from app.agent.nodes.triage import BODY_CLOSE, BODY_OPEN, SUBJECT_CLOSE, SUBJECT_OPEN
from app.agent.resilience import call_llm
from app.agent.state import AgentState
from app.config import settings
from app.llm.base import LLMClient

PROMPT_VERSION = "support_v1"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_VERSION}.txt"


class ClaimSupport(BaseModel):
    claim: str = Field(min_length=1, max_length=500)
    kb_id: str = Field(min_length=1, max_length=50)


class SupportCheckOutput(BaseModel):
    supported: bool
    conflicting: bool = False
    reason: str = Field(min_length=1, max_length=500)
    claims: list[ClaimSupport] = Field(default_factory=list)

    @classmethod
    def stub_example(cls) -> dict[str, Any]:
        return {
            "supported": True,
            "conflicting": False,
            "reason": "The retrieved chunks cover the customer's question.",
            "claims": [],
        }


@lru_cache(maxsize=4)
def _system_prompt(version: str = PROMPT_VERSION) -> str:
    prompt = _PROMPT_FILE.read_text(encoding="utf-8").strip()
    # Invariant 5 guard: static system prompt never carries ticket delimiters.
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


def make_grounding_node(pool: Any, client: LLMClient):
    def grounding(state: AgentState) -> dict[str, Any]:
        guard = step_guard(state)
        if guard is not None:
            return guard
        ticket_id = str(state["ticket_id"])
        trace_id = str(state["trace_id"])
        chunks = state.get("retrieved") or []
        best_score = max((float(c.get("rrf_score") or 0.0) for c in chunks), default=0.0)

        # Gate 1: retrieval confidence (deterministic, no LLM needed to fail).
        if not chunks or best_score < settings.grounding_threshold:
            trace_node(
                pool, ticket_id, trace_id, "grounding_check", "decision",
                {"best_rrf_score": best_score, "threshold": settings.grounding_threshold},
                {"supported": False, "reason_code": "LOW_CONFIDENCE"},
            )
            return {
                **escalation_update(
                    "LOW_CONFIDENCE",
                    f"best retrieval score {best_score:.4f} below threshold "
                    f"{settings.grounding_threshold:.4f}",
                ),
                "step_count": next_step(state),
            }

        # Gate 2: LLM support check with claim->kb_id mapping, validated in code.
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _user_content(state["subject"], state["body"], chunks)},
        ]
        response = call_llm(
            lambda: client.generate(messages, schema=SupportCheckOutput),
            pool=pool, ticket_id=ticket_id, trace_id=trace_id, node="grounding_check",
        )
        verdict = response.structured
        if not isinstance(verdict, SupportCheckOutput):
            raise TypeError("support check produced no structured output")

        retrieved_ids = {str(chunk["kb_id"]) for chunk in chunks}
        supported_claims = [
            claim for claim in verdict.claims if str(claim.kb_id) in retrieved_ids
        ]
        unsupported = len(verdict.claims) - len(supported_claims)
        supported = verdict.supported and unsupported == 0
        trace_node(
            pool, ticket_id, trace_id, "grounding_check", "llm",
            {"prompt_version": PROMPT_VERSION, "best_rrf_score": best_score},
            {"supported": supported, "conflicting": verdict.conflicting,
             "reason": verdict.reason, "unsupported_claims": unsupported},
            duration_ms=response.usage.latency_ms,
            tokens_in=response.usage.input_tokens, tokens_out=response.usage.output_tokens,
            cache_hit=response.cache_hit,
        )

        # Gate 3: conflicting sources.
        if verdict.conflicting:
            return {
                **escalation_update(
                    "CONFLICTING_SOURCES", "retrieved chunks contradict each other"
                ),
                "step_count": next_step(state),
            }
        if not supported:
            return {
                **escalation_update(
                    "LOW_CONFIDENCE", verdict.reason or "retrieved chunks do not answer the ticket"
                ),
                "step_count": next_step(state),
            }
        return {
            "grounding": {
                "supported": True,
                "prompt_version": PROMPT_VERSION,
                "best_rrf_score": best_score,
                "claims": [claim.model_dump() for claim in supported_claims],
            },
            "step_count": next_step(state),
        }

    return grounding
