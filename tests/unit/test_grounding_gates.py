"""Pure unit tests for the deterministic gates: citation validation, the
escalation update shape, and the step-limit guard (no DB needed)."""

from __future__ import annotations

import pytest

from app.agent.nodes.common import escalation_update, step_guard
from app.agent.nodes.draft import CitationError, validate_citations
from app.agent.nodes.grounding import resolve_recency_conflict
from app.config import settings

RETRIEVED = {"KB-006", "KB-007", "KB-009"}


def test_validate_citations_accepts_retrieved_only() -> None:
    reply = "Refunds follow the window rules [KB-006] and annual plans too [KB-007]."
    assert validate_citations(reply, ["KB-009"], RETRIEVED) == ["KB-006", "KB-007", "KB-009"]


def test_validate_citations_rejects_invented_kb_id() -> None:
    with pytest.raises(CitationError):
        validate_citations("Trust me [KB-999].", [], RETRIEVED)


def test_validate_citations_rejects_invented_claimed_citation() -> None:
    with pytest.raises(CitationError):
        validate_citations("No inline markers.", ["KB-999"], RETRIEVED)


def test_validate_citations_allows_uncited_draft() -> None:
    assert validate_citations("A reply with no citations at all.", [], RETRIEVED) == []


def test_escalation_update_carries_reason_and_queue() -> None:
    update = escalation_update("LOW_CONFIDENCE", "too weak", queue="billing")
    assert update["outcome"] == "escalated"
    assert update["escalation"]["reason_code"] == "LOW_CONFIDENCE"
    assert update["escalation"]["detail"] == "too weak"
    assert update["escalation"]["queue"] == "billing"


def test_step_guard_triggers_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "step_limit", 3)
    assert step_guard({"step_count": 2}) is None
    guard = step_guard({"step_count": 3})
    assert guard is not None
    assert guard["escalation"]["reason_code"] == "STEP_LIMIT"
    assert guard["outcome"] == "escalated"


def test_recency_conflict_keeps_unique_newest_source() -> None:
    chunks = [
        {"kb_id": "KB-006", "updated": "2026-01-01"},
        {"kb_id": "KB-007", "updated": "2025-01-01"},
        {"kb_id": "KB-009", "updated": "2026-02-01"},
    ]
    retained, resolved = resolve_recency_conflict(chunks, ["KB-006", "KB-007"])
    assert resolved is True
    assert [chunk["kb_id"] for chunk in retained] == ["KB-006", "KB-009"]


def test_recency_conflict_escalates_on_tied_dates() -> None:
    chunks = [
        {"kb_id": "KB-006", "updated": "2026-01-01"},
        {"kb_id": "KB-007", "updated": "2026-01-01"},
    ]
    retained, resolved = resolve_recency_conflict(chunks, ["KB-006", "KB-007"])
    assert resolved is False
    assert retained == chunks
