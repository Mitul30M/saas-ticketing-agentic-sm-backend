"""Render an eval run into a markdown report (PLAN 4.11).

    uv run python -m eval.report                     # newest run in eval/results
    uv run python -m eval.report eval/results/<run>.json

Writes `<run>.md` next to the JSON and prints the same table, so a published
number always has both a machine-readable and a human-readable copy from one
source.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eval.metrics import compute, render_markdown

RESULTS_DIR = Path("eval/results")


def newest(directory: Path) -> Path:
    """Newest *run* file. `eval/results/` also holds the ablation JSON, and
    picking that by mtime made `make eval` fail after a successful run."""
    for candidate in sorted(directory.glob("*.json"), reverse=True):
        try:
            payload = json.loads(candidate.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "tickets" in payload and "manifest" in payload:
            return candidate
    raise SystemExit(f"no run results in {directory}; run `python -m eval.run_eval` first")


def build(path: Path) -> tuple[dict[str, Any], str]:
    payload = json.loads(path.read_text())
    if "tickets" not in payload or "manifest" not in payload:
        raise SystemExit(f"{path} is not a run_eval result file")
    metrics = compute(payload["tickets"], payload["manifest"])
    return metrics, render_markdown(metrics)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render an eval run as markdown")
    parser.add_argument("results", nargs="?", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    path = Path(args.results) if args.results else newest(RESULTS_DIR)
    metrics, markdown = build(path)
    out = Path(args.out) if args.out else path.with_suffix(".md")
    out.write_text(markdown + "\n")
    print(markdown)
    print(f"\nwritten: {out}")

    unreviewed = metrics["dataset"]["unreviewed_ids"]
    if unreviewed:
        print(
            f"\nNOTE: {len(unreviewed)} of {metrics['dataset']['n']} rows are not "
            f"hand-reviewed ({', '.join(unreviewed)}); this is not a publishable headline."
        )
    if metrics["manifest"]["provider"] == "stub":
        print("\nNOTE: stub provider — plumbing only, not a measure of model quality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
