"""Pure unit tests for the deterministic gates: citation validation, the
escalation update shape, and the step-limit guard (no DB needed)."""

from __future__ import annotations

import pytest

from app.agent.nodes.common import escalation_update, step_guard
from app.agent.nodes.draft import CitationError, rejects_promise_language, validate_citations
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


def test_validate_citations_passes_uncited_draft_but_the_draft_node_rejects_it() -> None:
    """The pure validator accepts an empty citation set (it gates citABILITY);
    the draft NODE additionally rejects any draft whose citation union is
    empty (make_draft_node: `if not citations: raise CitationError`). This
    test pins the validator half of that split."""
    assert validate_citations("A reply with no citations at all.", [], RETRIEVED) == []


@pytest.mark.parametrize(
    "reply",
    [
        "Good news: your refund has been approved and will arrive soon [KB-006].",
        "We have issued your refund of $50 [KB-006].",
        "Your plan will be upgraded to Business right away [KB-011].",
        "I have upgraded your plan to Business [KB-011].",
        "Your subscription has been cancelled as requested [KB-009].",
    ],
)
def test_promise_language_is_denied(reply: str) -> None:
    assert rejects_promise_language(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "Refunds follow the 30-day window described in the policy [KB-006].",
        "You can request a plan change from the billing page; it needs approval [KB-011].",
        "To cancel, contact support through the dashboard [KB-009].",
    ],
)
def test_policy_explanations_are_not_promise_language(reply: str) -> None:
    assert rejects_promise_language(reply) is False


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
