"""P3-6 #2/#3: pure, deterministic parsers for refund amounts and plan targets.

Ambiguous input raises ValueError; the orchestration node converts that into
NEEDS_HUMAN_JUDGEMENT escalation. Amounts accept thousands separators; several
DISTINCT amounts are ambiguous. Plan targets: exactly one mentioned plan, or a
directional from/to reading where only the two named plans appear."""

from __future__ import annotations

import pytest

from app.agent.nodes.orchestration import parse_amount_cents, parse_target_plan


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("please refund $50", 5000),
        ("I paid $1,200 for this", 120_000),
        ("charge of $1,200.50 appeared twice", 120_050),
        ("refund the $0.50 charge", 50),
        ("$ 75 was taken", 7500),
    ],
)
def test_parse_amount_cents(text: str, expected: int) -> None:
    assert parse_amount_cents(text) == expected


@pytest.mark.parametrize("text", ["$,", "$", "$1,2"])
def test_malformed_amount_text_does_not_crash_the_parser(text: str) -> None:
    """P4-0.6: '$,' used to match the old regex and crash on float(''). The
    tightened regex matches only proper amounts (commas must be thousands
    separators, so '$1,2' does not match) and never raises float('') — the
    node escalates NEEDS_HUMAN_JUDGEMENT instead."""
    from app.agent.nodes.orchestration import AMOUNT_RE

    assert AMOUNT_RE.search(text) is None
    with pytest.raises(ValueError):
        parse_amount_cents(text)


@pytest.mark.parametrize(
    "text",
    [
        "refund $50 and $75 please",
        "you took $1,200 and also $1,201",
        "no dollar amounts here",
    ],
)
def test_ambiguous_amounts_raise(text: str) -> None:
    with pytest.raises(ValueError):
        parse_amount_cents(text)


def test_repeated_same_amount_is_not_ambiguous() -> None:
    assert parse_amount_cents("you charged $50 twice, refund the $50") == 5000


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("please upgrade me to Business", "PLAN-BUSINESS"),
        ("switch me to starter", "PLAN-STARTER"),
        ("I want the free plan", "PLAN-FREE"),
        ("move to business from starter", "PLAN-BUSINESS"),
        ("change my plan from Pro to Starter", "PLAN-STARTER"),
    ],
)
def test_parse_target_plan(text: str, expected: str) -> None:
    assert parse_target_plan(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "from Pro to Starter, not Business",
        "no plan named here at all",
        "upgrade me to pro or business, you pick",
    ],
)
def test_ambiguous_plan_targets_raise(text: str) -> None:
    with pytest.raises(ValueError):
        parse_target_plan(text)
