"""P4-0.4: data/dev (tuning) and data/eval (final report) must stay disjoint
(AGENTS §6). No row may appear in both files, and every dev row must carry the
hand label kb_covered consumed by scripts/grounding_report.py.

The eval side is now two files: the seed-exported `handwritten.jsonl` and the
gold-labelled `eval_set.jsonl` actually used by the runner. Checking only the
first would let the real eval set overlap dev silently.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEV = Path("data/dev/dev_tickets.jsonl")
EVAL_FILES = [Path("data/eval/handwritten.jsonl"), Path("data/eval/eval_set.jsonl")]


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _ticket(path: Path, row: dict[str, object]) -> dict[str, object]:
    """eval_set rows nest the ticket; handwritten rows are the ticket."""
    ticket = row.get("ticket")
    return ticket if isinstance(ticket, dict) else row


def _content_hash(ticket: dict[str, object]) -> str:
    canonical = json.dumps(
        {"subject": ticket.get("subject"), "body": ticket.get("body")}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _provenance(row: dict[str, object]) -> tuple[object, object] | None:
    if row.get("source_row") is None:
        return None
    return (row.get("source_dataset"), row.get("source_row"))


def test_dev_and_eval_files_share_no_content() -> None:
    dev = _rows(DEV)
    dev_hashes = {_content_hash(row) for row in dev}
    for path in EVAL_FILES:
        eval_hashes = {_content_hash(_ticket(path, row)) for row in _rows(path)}
        overlap = dev_hashes & eval_hashes
        assert overlap == set(), f"{len(overlap)} rows in {path} also appear in dev"


def test_dev_and_eval_share_no_source_rows() -> None:
    """Same dataset row = same ticket, even if the text was normalised."""
    dev_provenance = {
        key for key in (_provenance(row) for row in _rows(DEV)) if key is not None
    }
    eval_provenance = set()
    for path in EVAL_FILES:
        for row in _rows(path):
            key = _provenance(_ticket(path, row))
            if key is not None:
                eval_provenance.add(key)
    overlap = dev_provenance & eval_provenance
    assert overlap == set(), f"{len(overlap)} dataset rows are used by both splits: {overlap}"


def test_dev_rows_are_kb_labeled() -> None:
    rows = _rows(DEV)
    assert len(rows) >= 20
    uncovered = [row for row in rows if not row["kb_covered"]]
    assert len(uncovered) >= 8, "need >= 8 uncovered dev tickets (P4-0.4)"
    for row in rows:
        assert row["kb_covered"] in (True, False)
