"""Dev-split triage accuracy (the signal triage prompts were missing).

    make dev-triage
    (or) uv run python -m scripts.dev_triage_report --pause-seconds 6

`data/dev/` never had gold triage labels — only `kb_covered` and the source
dataset's noisy hints, which AGENTS §6 forbids using. So every triage prompt
change had to be judged on the eval split, which is exactly the tuning we are
supposed to avoid. This script measures triage on dev against
`data/dev/dev_triage_gold.jsonl`, and refuses to run while any row is
unreviewed."""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from app.agent.nodes.triage import PROMPT_VERSION, triage_ticket
from app.config import settings
from app.llm.base import LLMClient
from eval.metrics import _classification

DEV_FILE = Path("data/dev/dev_tickets.jsonl")
GOLD_FILE = Path("data/dev/dev_triage_gold.jsonl")
OUT_FILE = Path("eval/results/triage_dev.csv")

_PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2, "urgent": 3}


def _load() -> list[dict]:
    tickets = [json.loads(line) for line in DEV_FILE.read_text().splitlines() if line.strip()]
    gold = [json.loads(line) for line in GOLD_FILE.read_text().splitlines() if line.strip()]
    by_subject = {row["subject"]: row for row in gold}
    merged = []
    for ticket in tickets:
        row = by_subject.get(ticket["subject"])
        if row is None:
            raise SystemExit(f"no triage gold for dev ticket: {ticket['subject']!r}")
        merged.append({"ticket": ticket, "gold": row})
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Dev-split triage accuracy")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pause-seconds", type=float, default=0.0,
                        help="sleep between calls (free tiers cap at 8k tokens/minute)")
    parser.add_argument("--include-unreviewed", action="store_true",
                        help="measure against unreviewed draft labels (debug only)")
    args = parser.parse_args()

    rows = _load()
    unreviewed = [row["gold"]["dev_id"] for row in rows if not row["gold"]["reviewed"]]
    if unreviewed and not args.include_unreviewed:
        raise SystemExit(
            f"{len(unreviewed)} dev triage label(s) are not hand-reviewed "
            f"({', '.join(unreviewed)}); review them, set reviewed=true, or pass "
            "--include-unreviewed to measure a draft."
        )
    if args.limit is not None:
        rows = rows[: args.limit]

    client = LLMClient(pool=None)  # uses LLM_PROVIDER / LLM_MODEL from .env
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with OUT_FILE.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "dev_id", "subject", "gold_category", "actual_category",
            "gold_priority", "actual_priority", "gold_sentiment", "actual_sentiment",
            "prompt_version",
        ])
        writer.writeheader()
        for row in rows:
            ticket, gold = row["ticket"], row["gold"]["gold"]
            if args.pause_seconds:
                time.sleep(args.pause_seconds)
            verdict = triage_ticket(client, ticket["subject"], ticket["body"])
            result = {
                "dev_id": row["gold"]["dev_id"],
                "subject": ticket["subject"],
                "gold_category": gold["category"], "actual_category": verdict.category.value,
                "gold_priority": gold["priority"], "actual_priority": verdict.priority.value,
                "gold_sentiment": gold["sentiment"], "actual_sentiment": verdict.sentiment.value,
                "prompt_version": PROMPT_VERSION,
            }
            writer.writerow(result)
            results.append(result)

    print(f"prompt: {PROMPT_VERSION}  provider: {settings.llm_provider}  "
          f"model: {settings.llm_model or '(stub)'}")
    for field in ("category", "priority", "sentiment"):
        pairs = [(row[f"gold_{field}"], row[f"actual_{field}"]) for row in results]
        stats = _classification(pairs)
        print(f"  {field:10} accuracy {stats['accuracy']:.3f}  macro-F1 {stats['macro_f1']:.3f}")
    within_one = sum(
        1 for row in results
        if abs(_PRIORITY_ORDER[row["gold_priority"]] - _PRIORITY_ORDER[row["actual_priority"]]) <= 1
    )
    print(f"  priority within-one {within_one / len(results):.3f}")
    misses = [row for row in results if row["gold_category"] != row["actual_category"]]
    if misses:
        print("  category misses:")
        for row in misses:
            print(f"    {row['dev_id']} {row['gold_category']:20} -> {row['actual_category']:20} "
                  f"| {row['subject'][:52]}")
    print(f"wrote {OUT_FILE} ({len(results)} rows)")


if __name__ == "__main__":
    main()
