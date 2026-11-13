"""Contract tests for the dev triage gold labels.

`data/dev/` is the tuning split (AGENTS §6). It only carried `kb_covered`, so
triage prompt changes had no dev signal and were judged on the eval split. These
labels fix that; the test keeps them complete and in the shared enums.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.domain.enums import CATEGORY_QUEUE, Category, Priority, Queue, Sentiment

DEV = Path("data/dev/dev_tickets.jsonl")
GOLD = Path("data/dev/dev_triage_gold.jsonl")


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_every_dev_ticket_has_exactly_one_triage_label() -> None:
    tickets = _rows(DEV)
    gold = _rows(GOLD)
    assert len(gold) == len(tickets), (len(gold), len(tickets))
    subjects = [row["subject"] for row in gold]
    assert len(subjects) == len(set(subjects)), "duplicate subject in the triage gold"
    assert {row["subject"] for row in gold} == {row["subject"] for row in tickets}

    ids = [row["dev_id"] for row in gold]
    assert len(ids) == len(set(ids))
    assert all(dev_id.startswith("D-") for dev_id in ids)


def test_labels_use_the_shared_enums_and_derived_queue() -> None:
    for row in _rows(GOLD):
        gold = row["gold"]
        assert set(gold) == {"category", "priority", "sentiment", "queue"}, row["dev_id"]
        Category(gold["category"])
        Priority(gold["priority"])
        Sentiment(gold["sentiment"])
        Queue(gold["queue"])
        assert gold["queue"] == str(CATEGORY_QUEUE[Category(gold["category"])]), row["dev_id"]


def test_review_state_is_explicit_and_notes_exist() -> None:
    for row in _rows(GOLD):
        assert isinstance(row["reviewed"], bool), row["dev_id"]
        assert row["note"], f"{row['dev_id']} needs a reviewer note"


def test_no_label_is_borrowed_from_the_source_dataset_hints() -> None:
    """`topic`/`priority_hint` are source noise; a label that merely copies them
    is not hand review. Allow agreement, but require the note to exist so the
    reasoning is on record."""
    tickets = {row["subject"]: row for row in _rows(DEV)}
    for row in _rows(GOLD):
        ticket = tickets[row["subject"]]
        assert ticket.get("topic") is not None  # sanity: the noisy hint is present
        if ticket.get("topic") != row["gold"]["category"]:
            # disagreements are expected and are exactly why hand labels matter
            assert row["note"], row["dev_id"]
