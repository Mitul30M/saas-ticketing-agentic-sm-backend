"""P3-1: the resume payload IS the human decision; parsing must fail closed.

Anything that is not exactly {"approved": True, "decided_by": <non-blank str>}
is a rejection, never consent. edited_text is honoured only when it is a
non-blank string within the draft size cap."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.nodes.approval import parse_decision


@pytest.mark.parametrize("value", ["true", "false", "yes", 1, 0, None, [], {}])
def test_approved_value_must_be_literal_true(value: Any) -> None:
    decision = parse_decision({"approved": value, "decided_by": "mia"})
    assert decision.approved is False
    assert decision.decided_by == "invalid_resume_payload"


def test_valid_approval() -> None:
    decision = parse_decision({"approved": True, "decided_by": "mia"})
    assert decision.approved is True
    assert decision.decided_by == "mia"
    assert decision.edited_text is None


@pytest.mark.parametrize("raw", [{"approved": True}, {"approved": True, "decided_by": "  "}])
def test_missing_or_blank_decided_by_is_a_rejection(raw: dict[str, Any]) -> None:
    decision = parse_decision(raw)
    assert decision.approved is False
    assert decision.decided_by == "invalid_resume_payload"


@pytest.mark.parametrize("bad", [123, "", "   ", "x" * 5001, None])
def test_invalid_edited_text_is_ignored_not_a_rejection(bad: Any) -> None:
    decision = parse_decision({"approved": True, "decided_by": "mia", "edited_text": bad})
    assert decision.approved is True
    assert decision.edited_text is None


def test_valid_edited_text_is_kept() -> None:
    decision = parse_decision(
        {"approved": True, "decided_by": "mia", "edited_text": "edited reply"}
    )
    assert decision.approved is True
    assert decision.edited_text == "edited reply"


@pytest.mark.parametrize("raw", ["yes please", None, [], 1])
def test_non_dict_payloads_never_authorize(raw: Any) -> None:
    decision = parse_decision(raw)
    assert decision.approved is False
    assert decision.decided_by == "invalid_resume_payload"


def test_edited_text_at_cap_is_kept_above_cap_is_dropped() -> None:
    assert parse_decision(
        {"approved": True, "decided_by": "mia", "edited_text": "x" * 4000}
    ).edited_text == "x" * 4000
    assert parse_decision(
        {"approved": True, "decided_by": "mia", "edited_text": "x" * 4001}
    ).edited_text is None
