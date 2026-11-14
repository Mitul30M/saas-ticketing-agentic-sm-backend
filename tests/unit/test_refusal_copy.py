"""The refusal line appended to a draft when an action is refused.

The draft is generated before the action is attempted, so on a refusal it can
read as if a proposal exists. The sentence is deterministic (no LLM call) and
customer-safe: it must never repeat internal numbers such as balances.
"""

from __future__ import annotations

import pytest

from app.agent.nodes.orchestration import refusal_sentence


@pytest.mark.parametrize("code", ["business_rule", "ownership_mismatch", "duplicate", None, "timeout"])
def test_every_code_produces_a_review_sentence(code: str | None) -> None:
    sentence = refusal_sentence("refund", code)
    assert sentence
    assert "support specialist will review" in sentence
    assert sentence[0].isupper() and sentence.endswith(".")


def test_refund_and_plan_change_are_named_for_a_business_rule() -> None:
    assert "this refund" in refusal_sentence("refund", "business_rule")
    assert "this plan change" in refusal_sentence("plan_change", "business_rule")


def test_identity_and_duplicate_get_their_own_wording() -> None:
    assert "verify" in refusal_sentence("refund", "ownership_mismatch")
    assert "already an open request" in refusal_sentence("refund", "duplicate")


def test_no_internal_numbers_are_leaked() -> None:
    for code in ("business_rule", "ownership_mismatch", "duplicate", "invalid_input", None):
        sentence = refusal_sentence("refund", code)
        assert "cents" not in sentence.lower()
        assert not any(char.isdigit() for char in sentence)
