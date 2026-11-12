"""P4-0.4: data/dev (tuning) and data/eval (final report) must stay disjoint
(AGENTS §6). No row may appear in both files, and every dev row must carry the
hand label kb_covered consumed by scripts/grounding_report.py."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEV = Path("data/dev/dev_tickets.jsonl")
EVAL = Path("data/eval/handwritten.jsonl")


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _content_hash(row: dict[str, object]) -> str:
    canonical = json.dumps(
        {"subject": row.get("subject"), "body": row.get("body")}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_dev_and_eval_files_share_no_content() -> None:
    dev_hashes = {_content_hash(row) for row in _rows(DEV)}
    eval_hashes = {_content_hash(row) for row in _rows(EVAL)}
    overlap = dev_hashes & eval_hashes
    assert overlap == set(), f"{len(overlap)} rows appear in both dev and eval"


def test_dev_rows_are_kb_labeled() -> None:
    rows = _rows(DEV)
    assert len(rows) >= 20
    uncovered = [row for row in rows if not row["kb_covered"]]
    assert len(uncovered) >= 8, "need >= 8 uncovered dev tickets (P4-0.4)"
    for row in rows:
        assert row["kb_covered"] in (True, False)
