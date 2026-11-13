"""Flip the review flag on specific eval rows without regenerating the file.

    uv run python -m eval.review mark E-001 E-002 --by mitul
    uv run python -m eval.review list

Kept deliberately small: the reviewed flags are reviewer state, and they must
survive any edit to the labels.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

EVAL_SET = Path("data/eval/eval_set.jsonl")
REVIEWED_ON = "2026-11-13"


def load() -> list[dict[str, Any]]:
    return [json.loads(line) for line in EVAL_SET.read_text().splitlines() if line.strip()]


def save(rows: list[dict[str, Any]]) -> None:
    EVAL_SET.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def main() -> None:
    parser = argparse.ArgumentParser(description="manage eval-set review flags")
    sub = parser.add_subparsers(dest="command", required=True)

    mark = sub.add_parser("mark", help="mark ids as hand-reviewed")
    mark.add_argument("ids", nargs="+")
    mark.add_argument("--by", required=True)
    mark.add_argument("--undo", action="store_true", help="clear the flag instead")

    sub.add_parser("list", help="show reviewed/unreviewed counts")

    args = parser.parse_args()
    rows = load()

    if args.command == "list":
        reviewed = [row["id"] for row in rows if row["reviewed"]]
        pending = [row["id"] for row in rows if not row["reviewed"]]
        print(f"reviewed {len(reviewed)}/{len(rows)}: {' '.join(reviewed) or '-'}")
        print(f"pending  {len(pending)}: {' '.join(pending) or '-'}")
        return

    known = {row["id"] for row in rows}
    unknown = [i for i in args.ids if i not in known]
    if unknown:
        raise SystemExit(f"unknown eval ids: {unknown}")
    for row in rows:
        if row["id"] in set(args.ids):
            row["reviewed"] = not args.undo
            row["reviewed_by"] = None if args.undo else args.by
            row["reviewed_on"] = None if args.undo else REVIEWED_ON
    save(rows)
    print(f"{'cleared' if args.undo else 'marked'} {len(args.ids)} row(s)")


if __name__ == "__main__":
    main()
