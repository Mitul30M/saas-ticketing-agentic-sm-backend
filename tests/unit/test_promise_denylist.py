"""P4-0.2: the promise-language deny-list is defense in depth, NOT the
control (the human approval gate is the control — docs/DECISIONS.md). These
tests pin that the known commitment phrasings are rejected, that real KB text
never trips it (false-positive guard), and that neutral policy sentences pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.nodes.draft import PROMISE_PATTERNS, rejects_promise_language

KB_DIR = Path(__file__).resolve().parents[2] / "data" / "kb"


@pytest.mark.parametrize(
    "reply",
    [
        "We will process your refund within 3 business days [KB-006].",
        "I've initiated a refund for your invoice [KB-006].",
        "You will receive a full refund shortly [KB-006].",
        "We'll refund the $50 right away [KB-006].",
        "I will issue your refund today [KB-006].",
        "We are going to send your refund this week [KB-006].",
        "We initiated your refund immediately [KB-006].",
        "You'll get your refund in a few days [KB-006].",
        "I have refunded $20.03 to your card [KB-006].",
        "We've refunded the full amount [KB-006].",
    ],
)
def test_commitment_phrasings_are_denied(reply: str) -> None:
    assert rejects_promise_language(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "You can request a refund within 30 days [KB-006].",
        "Refunds are processed within 3 business days [KB-006].",
        "A refund request must be approved by a human reviewer [KB-006].",
        "Plan changes take effect at the next billing cycle [KB-011].",
        "Requests for a refund outside the window need support review [KB-006].",
    ],
)
def test_neutral_policy_sentences_pass(reply: str) -> None:
    assert rejects_promise_language(reply) is False


def _sentences(text: str) -> list[str]:
    import re

    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def test_no_kb_article_sentence_trips_the_denylist() -> None:
    """Guard against false positives: every sentence of every published KB
    article must pass the deny-list, since drafts cite KB wording."""
    kb_files = sorted(KB_DIR.glob("KB-*.md"))
    assert len(kb_files) >= 30, "KB corpus missing"
    offenders: list[str] = []
    for path in kb_files:
        for sentence in _sentences(path.read_text(encoding="utf-8")):
            for pattern in PROMISE_PATTERNS:
                if pattern.search(sentence):
                    offenders.append(f"{path.name}: {sentence[:120]} ({pattern.pattern})")
    assert offenders == []
