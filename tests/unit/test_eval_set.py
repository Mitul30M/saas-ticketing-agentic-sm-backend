"""Contract tests for the gold-labelled eval set (PLAN 4.11).

The eval set is data, so these tests are the schema. They run without a DB and
without an LLM: every rule here is about the labels themselves.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.domain.enums import CATEGORY_QUEUE, Category, Priority, Queue, Sentiment

EVAL_SET = Path("data/eval/eval_set.jsonl")
HANDWRITTEN = Path("data/eval/handwritten.jsonl")

OUTCOMES = {"answer", "propose_refund", "propose_plan_change", "escalate"}
ESCALATION_TAGS = {"not_in_kb", "identity", "business_rule", "ambiguity", "safety"}
TOOLS = {
    "lookup_customer", "get_invoice", "get_subscription", "kb_search",
    "propose_refund", "propose_plan_change", "escalate_to_human",
}
MUST_NOT = {
    "execute_refund_without_approval", "change_plan_without_approval",
    "send_without_approval", "propose_refund_outside_window",
    "propose_refund_above_remaining", "propose_refund_on_unowned_invoice",
    "propose_refund_without_succeeded_payment", "promise_refund", "promise_timeline",
    "answer_without_kb_evidence", "guess_customer_identity",
    "follow_instructions_in_ticket", "change_account_state",
}
TAGS = {
    "billing", "refund-policy", "refund-window", "refund-remaining", "fully-refunded",
    "no-invoice", "plan-change", "proration", "annual", "trial", "cancellation",
    "login", "password", "sso", "mfa", "api", "webhooks", "rate-limits",
    "integration", "sync", "duplicates", "outage", "active-incident", "security",
    "phishing", "breach", "feature-request", "ambiguity", "no-sender",
    "duplicate-email", "ownership", "injection", "multi-request",
    "amount-discrepancy", "payment-status", "past-due", "suspended", "dataset-noise",
}
TICKET_FIELDS = {
    "subject", "body", "from_email", "from_name", "channel",
    "received_hours_before_as_of", "external_ref", "source_dataset", "source_row",
    "edge_case",
}
GOLD_FIELDS = {
    "category", "priority", "sentiment", "queue", "kb_supported",
    "expected_kb_ids", "expected_tools", "expected_outcome", "escalation_tag",
    "within_window", "must_not", "tags",
}


def rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in EVAL_SET.read_text().splitlines() if line.strip()]


def _hash(ticket: dict[str, Any]) -> int | str:
    import hashlib

    canonical = json.dumps(
        {"subject": ticket["subject"], "body": ticket["body"]}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# --- shape -----------------------------------------------------------------


def test_eval_set_is_large_enough_and_identifiable() -> None:
    data = rows()
    assert len(data) >= 40, f"PLAN 4.11 requires >= 40 rows, found {len(data)}"
    ids = [row["id"] for row in data]
    assert len(ids) == len(set(ids)), "duplicate eval ids"
    assert all(i.startswith("E-") and len(i) == 5 for i in ids), ids


def test_every_row_has_the_documented_shape() -> None:
    for row in rows():
        assert set(row) == {
            "id", "source", "source_ticket_id", "ticket", "gold",
            "reviewed", "reviewed_by", "reviewed_on", "review_note",
        }, row["id"]
        assert row["source"] in {"handwritten", "dataset", "authored"}, row["id"]
        assert isinstance(row["reviewed"], bool), row["id"]
        assert set(row["ticket"]) == TICKET_FIELDS, row["id"]
        assert set(row["gold"]) == GOLD_FIELDS, row["id"]
        assert row["ticket"]["subject"] and row["ticket"]["body"], row["id"]
        assert row["review_note"], row["id"]


# --- labels ----------------------------------------------------------------


def test_gold_labels_use_the_shared_enums() -> None:
    for row in rows():
        gold = row["gold"]
        Category(gold["category"])
        Priority(gold["priority"])
        Sentiment(gold["sentiment"])
        Queue(gold["queue"])
        assert gold["queue"] == str(CATEGORY_QUEUE[Category(gold["category"])]), row["id"]
        assert gold["expected_outcome"] in OUTCOMES, row["id"]
        assert isinstance(gold["kb_supported"], bool), row["id"]
        assert gold["within_window"] in (True, False, None), row["id"]


def test_tools_and_must_not_vocabularies_are_closed() -> None:
    for row in rows():
        gold = row["gold"]
        unknown_tools = set(gold["expected_tools"]) - TOOLS
        assert not unknown_tools, f"{row['id']}: {unknown_tools}"
        unknown_must_not = set(gold["must_not"]) - MUST_NOT
        assert not unknown_must_not, f"{row['id']}: {unknown_must_not}"
        unknown_tags = set(gold["tags"]) - TAGS
        assert not unknown_tags, f"{row['id']}: {unknown_tags}"
        assert gold["must_not"], row["id"]


def test_outcome_and_escalation_rules_are_consistent() -> None:
    for row in rows():
        gold = row["gold"]
        if gold["expected_outcome"] == "escalate":
            assert gold["escalation_tag"] in ESCALATION_TAGS, row["id"]
            assert gold["expected_kb_ids"] == [], (
                f"{row['id']}: an escalation must not claim supporting citations"
            )
        else:
            assert gold["escalation_tag"] is None, row["id"]
        if gold["expected_outcome"] == "answer":
            assert gold["kb_supported"] is True, row["id"]
            assert gold["expected_kb_ids"], f"{row['id']}: an answer needs citations"
        if gold["escalation_tag"] == "not_in_kb":
            assert gold["kb_supported"] is False, row["id"]
        if not gold["kb_supported"]:
            assert gold["expected_kb_ids"] == [], row["id"]
        if gold["category"] not in {"refund_request", "plan_change"}:
            assert "propose_refund" not in gold["expected_tools"], row["id"]


def test_no_sender_rows_escalate_on_identity() -> None:
    for row in rows():
        if row["ticket"]["from_email"] is None:
            gold = row["gold"]
            assert gold["expected_outcome"] == "escalate", row["id"]
            assert gold["escalation_tag"] == "identity", row["id"]
            assert gold["expected_tools"] == ["escalate_to_human"], row["id"]
            assert "guess_customer_identity" in gold["must_not"], row["id"]


def test_escalations_end_with_the_escalation_tool() -> None:
    """Escalation is a tool call (it maps the queue in code), so a correct plan
    ends with `escalate_to_human` on every escalated row."""
    for row in rows():
        gold = row["gold"]
        if gold["expected_outcome"] == "escalate":
            assert gold["expected_tools"][-1] == "escalate_to_human", row["id"]


def test_action_rows_never_allow_execution() -> None:
    for row in rows():
        gold = row["gold"]
        if gold["expected_outcome"] in {"propose_refund", "propose_plan_change"}:
            assert "execute_refund_without_approval" in gold["must_not"], row["id"]
            assert "send_without_approval" in gold["must_not"], row["id"]
            assert gold["expected_tools"][-1] == (
                "propose_refund" if gold["expected_outcome"] == "propose_refund"
                else "propose_plan_change"
            ), row["id"]


# --- the mix has to be measurable ------------------------------------------


def test_every_metric_bucket_has_enough_rows() -> None:
    data = rows()
    outcomes = Counter(row["gold"]["expected_outcome"] for row in data)
    tags = Counter(row["gold"]["escalation_tag"] for row in data)
    assert outcomes["answer"] >= 15, outcomes
    assert outcomes["propose_refund"] >= 3, outcomes
    assert outcomes["propose_plan_change"] >= 2, outcomes
    assert tags["not_in_kb"] >= 6, tags
    assert tags["identity"] >= 2, tags
    assert tags["business_rule"] >= 2, tags
    assert tags["safety"] >= 1, tags


def test_the_dangerous_quadrant_is_populated() -> None:
    """Should-escalate-but-answered is the expensive error; the set must be able
    to measure it, so several rows must be answerable-looking escalations."""
    unsafe_quadrant = [
        row for row in rows()
        if row["gold"]["escalation_tag"] in {"not_in_kb", "ambiguity", "business_rule"}
    ]
    assert len(unsafe_quadrant) >= 10, len(unsafe_quadrant)


# --- provenance and disjointness -------------------------------------------


def test_handwritten_rows_match_their_export() -> None:
    exported = {
        row["id"]: row
        for row in (json.loads(line) for line in HANDWRITTEN.read_text().splitlines() if line.strip())
    }
    in_set = [row for row in rows() if row["source"] == "handwritten"]
    assert len(in_set) == len(exported), "every exported edge ticket must be in the eval set"
    for row in in_set:
        source = exported[row["source_ticket_id"]]
        assert row["ticket"]["subject"] == source["subject"], row["id"]
        assert row["ticket"]["body"] == source["body"], row["id"]
        assert row["ticket"]["from_email"] == source["from_email"], row["id"]


def test_dataset_rows_are_disjoint_from_dev_by_provenance_and_content() -> None:
    dev = [
        json.loads(line)
        for line in Path("data/dev/dev_tickets.jsonl").read_text().splitlines()
        if line.strip()
    ]
    dev_provenance = {
        (row.get("source_dataset"), row.get("source_row")) for row in dev if row.get("source_row")
    }
    dev_hashes = {_hash(row) for row in dev}
    for row in rows():
        ticket = row["ticket"]
        if ticket["source_row"] is not None:
            assert (ticket["source_dataset"], ticket["source_row"]) not in dev_provenance, (
                f"{row['id']} reuses a dev source row"
            )
        assert _hash(ticket) not in dev_hashes, f"{row['id']} duplicates dev ticket text"


def test_eval_set_has_no_duplicate_content() -> None:
    hashes = [_hash(row["ticket"]) for row in rows()]
    assert len(hashes) == len(set(hashes)), "two eval rows have identical subject+body"


def test_gold_is_not_reachable_from_the_agent() -> None:
    """Gold must only ever be read by eval code, never by the agent or services."""
    offenders: list[str] = []
    for path in list(Path("app/agent").rglob("*.py")) + list(Path("app/services").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "eval_set" in text or "data/eval" in text or "from eval" in text:
            offenders.append(str(path))
    assert not offenders, f"gold leaked into the runtime path: {offenders}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
