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

PROMPT_VERSION = "support_v4"

# COVERAGE.md marks these categories as deliberately uncovered ("escalate"). The
# support check is an LLM and may still say yes to a neighbouring article — the
# gpt-oss-120b run drafted a billing reply for a possible account compromise
# (E-036). The boundary is policy, so it is enforced here, not asked for.
UNCOVERED_CATEGORIES: dict[str, str] = {
    "security_incident": "no security-incident runbook in the KB by design",
    "feature_request": "roadmap and feature commitments are not supported",
}
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / f"{PROMPT_VERSION}.txt"


class ClaimSupport(BaseModel):
    claim: str = Field(min_length=1, max_length=500)
    kb_id: str = Field(min_length=1, max_length=50)


class SupportCheckOutput(BaseModel):
    supported: bool
    conflicting: bool = False
    # Cosmetic: it feeds the trace and the dashboard, never a gate. gpt-oss-120b
    # omitted it on real tickets, which used to fail an otherwise valid verdict,
    # so the field defaults instead of being required.
    reason: str = Field(default="", max_length=500)
    claims: list[ClaimSupport] = Field(default_factory=list)
    conflicting_kb_ids: list[str] = Field(default_factory=list)

    @classmethod
    def stub_example(cls) -> dict[str, Any]:
        return {
            "supported": True,
            "conflicting": False,
            "reason": "The retrieved chunks cover the customer's question.",
            "claims": [{"claim": "The retrieved policy answers the question.", "kb_id": "KB-006"}],
            "conflicting_kb_ids": [],
        }


def resolve_recency_conflict(
    chunks: list[dict[str, Any]], conflicting_ids: list[str]
) -> tuple[list[dict[str, Any]], bool]:
    candidates = [chunk for chunk in chunks if str(chunk["kb_id"]) in set(conflicting_ids)]
    if len({str(chunk["kb_id"]) for chunk in candidates}) < 2:
        return chunks, False
    dates = {
        str(chunk["kb_id"]): str(chunk.get("updated") or "")
        for chunk in candidates
    }
    newest = max(dates.values())
    newest_ids = [kb_id for kb_id, updated in dates.items() if updated == newest]
    if len(newest_ids) != 1:
        return chunks, False
    retained = [
        chunk for chunk in chunks
        if str(chunk["kb_id"]) not in conflicting_ids or str(chunk["kb_id"]) == newest_ids[0]
    ]
    return retained, True


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

        # Gate 0: a category the KB deliberately does not cover.
        category = str((state.get("triage") or {}).get("category") or "")
        if category in UNCOVERED_CATEGORIES:
            trace_node(
                pool, ticket_id, trace_id, "grounding_check", "decision",
                {"category": category}, {"supported": False, "reason_code": "LOW_CONFIDENCE"},
            )
            return {
                **escalation_update("LOW_CONFIDENCE", UNCOVERED_CATEGORIES[category]),
                "step_count": next_step(state),
            }

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
        supported = verdict.supported and unsupported == 0 and bool(supported_claims)
        trace_node(
            pool, ticket_id, trace_id, "grounding_check", "llm",
            {"prompt_version": PROMPT_VERSION, "best_rrf_score": best_score},
            {"supported": supported, "conflicting": verdict.conflicting,
             "reason": verdict.reason, "unsupported_claims": unsupported},
            duration_ms=response.usage.latency_ms,
            tokens_in=response.usage.input_tokens, tokens_out=response.usage.output_tokens,
            cache_hit=response.cache_hit,
        )

        # Gate 3: conflicts are resolved only when the newest source is unique.
        if verdict.conflicting:
            resolved_chunks, resolved = resolve_recency_conflict(
                chunks, verdict.conflicting_kb_ids
            )
            if resolved:
                chunks = resolved_chunks
                supported_claims = [
                    claim for claim in supported_claims
                    if any(str(chunk["kb_id"]) == str(claim.kb_id) for chunk in chunks)
                ]
                supported = verdict.supported and bool(supported_claims) and not (
                    len(verdict.claims) > len(supported_claims)
                )
            if not resolved:
                return {
                    **escalation_update(
                        "CONFLICTING_SOURCES",
                        "retrieved sources conflict without one unambiguous newest policy",
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
