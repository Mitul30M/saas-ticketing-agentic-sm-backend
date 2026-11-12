"""Shared scripted-LLM test helpers (P4-0.5).

Stub mode is for plumbing/CI only (AGENTS §6); wherever a test asserts on
CONTENT (citations, claim kb_ids, verdicts) it uses one of these doubles so
nothing depends on the stub's canned examples. The support/draft claims use a
kb_id ACTUALLY PRESENT in the retrieved chunks (parsed from the user content),
so claim validation only fails when retrieval itself found nothing."""

from __future__ import annotations

import re
from typing import Any

from app.llm.base import LLMResponse, Usage


def retrieved_kb_ids(messages: Any) -> list[str]:
    ids: list[str] = []
    for message in messages:
        content = str(message.get("content", "")) if isinstance(message, dict) else str(message)
        for match in re.findall(r"kb_id=(KB-[A-Za-z0-9_-]+)", content):
            if match not in ids:
                ids.append(match)
    return ids


class ScriptedLLM:
    """Deterministic structured LLM double.

    `category` drives TriageOutput; `support` / `draft` are partial payloads
    merged over the defaults below (pass e.g. {"supported": False} to drive a
    grounding rejection, or explicit `claims` with an invented kb_id)."""

    def __init__(
        self,
        category: str = "invoice_question",
        support: dict[str, Any] | None = None,
        draft: dict[str, Any] | None = None,
    ) -> None:
        self.category = category
        self.support = support or {}
        self.draft = draft or {}

    def generate(self, messages: Any, schema: Any = None, **_: Any) -> LLMResponse:
        name = schema.__name__
        kb_ids = retrieved_kb_ids(messages)
        primary = kb_ids[0] if kb_ids else "KB-006"
        if name == "TriageOutput":
            action = self.category in {"refund_request", "plan_change"}
            payload = schema.model_validate({
                "category": self.category,
                "priority": "medium",
                "sentiment": "negative" if action else "neutral",
                "reasons": {
                    "category": "The customer requests an account action."
                    if action
                    else "The customer asks a routine policy question.",
                    "priority": "Routine billing matter.",
                    "sentiment": "The customer is dissatisfied."
                    if action
                    else "The tone is polite and neutral.",
                },
            })
        elif name == "SupportCheckOutput":
            merged: dict[str, Any] = {
                "supported": True,
                "conflicting": False,
                "reason": "The retrieved knowledge base chunks answer the request.",
                "claims": [{"claim": "The policy answers the request.", "kb_id": primary}],
                "conflicting_kb_ids": [],
            }
            merged.update(self.support)
            payload = schema.model_validate(merged)
        else:
            merged_draft: dict[str, Any] = {
                "reply": f"We can help with that under the current policy [{primary}].",
                "citations": [primary],
            }
            merged_draft.update(self.draft)
            payload = schema.model_validate(merged_draft)
        return LLMResponse("", payload, Usage())
