"""Retrieval ablation: BM25 / vector / hybrid / hybrid+rerank (PLAN 4.11).

    uv run python -m eval.retrieval_ablation            # needs the eval DB + KB

Two questions, two populations, deliberately kept apart:

* **Hit rate on the eval set** — gold `expected_kb_ids` only. Tuning never
  touches this split.
* **Threshold sweep on dev** — `kb_covered` labels only. tau is chosen here and
  then applied to eval, so the published threshold is not fitted on the report.

"hybrid+rerank" is an experiment, not a production change: the reranker is a
deterministic lexical tie-break (`rrf * (1 + title overlap)`) run only by this
script. The agent keeps using plain hybrid retrieval.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.db import create_pool
from app.retrieval.hybrid import (
    fts_search,
    hybrid_search,
    vector_search,
    warm_encoder,
)

EVAL_SET = Path("data/eval/eval_set.jsonl")
DEV = Path("data/dev/dev_tickets.jsonl")
OUT = Path("eval/results/retrieval_ablation")
WORD_RE = re.compile(r"[a-z0-9_]+")
TOP_K = 5
TAU_GRID = [round(0.008 + step * 0.002, 3) for step in range(16)]  # 0.008 .. 0.038


def _unique_ids(rows: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for row in rows:
        kb_id = str(row["kb_id"])
        if kb_id not in seen:
            seen.append(kb_id)
    return seen


def _bm25(conn: Any, subject: str, body: str) -> list[dict[str, Any]]:
    return fts_search(conn, f"{subject}\n{body}", TOP_K)


def _vector(conn: Any, subject: str, body: str) -> list[dict[str, Any]]:
    return vector_search(conn, f"{subject}\n{body}", TOP_K)


def _hybrid(conn: Any, subject: str, body: str) -> list[dict[str, Any]]:
    return [chunk.to_dict() for chunk in hybrid_search(conn, f"{subject}\n{body}", TOP_K)]


def _hybrid_rerank(conn: Any, subject: str, body: str) -> list[dict[str, Any]]:
    """Hybrid candidates, then a lexical tie-break towards the article title."""
    query_tokens = set(WORD_RE.findall(f"{subject} {body}".lower()))
    chunks = hybrid_search(conn, f"{subject}\n{body}", TOP_K * 2)
    scored: list[tuple[float, Any]] = []
    for chunk in chunks:
        title_tokens = set(WORD_RE.findall(str(chunk.title).lower()))
        overlap = len(query_tokens & title_tokens) / max(1, len(query_tokens))
        scored.append((chunk.rrf_score * (1 + overlap), chunk))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [chunk.to_dict() for _, chunk in scored[:TOP_K]]


VARIANTS: dict[str, Callable[[Any, str, str], list[dict[str, Any]]]] = {
    "bm25": _bm25,
    "vector": _vector,
    "hybrid": _hybrid,
    "hybrid+rerank (experimental)": _hybrid_rerank,
}


def _hit_metrics(ranked: list[str], expected: set[str]) -> tuple[int, int, float]:
    """(hit@k list is handled by caller, rank of first hit or 0)."""
    for position, kb_id in enumerate(ranked, start=1):
        if kb_id in expected:
            return position, 1, 1.0 / position
    return 0, 0, 0.0


def _eval_rows() -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in EVAL_SET.read_text().splitlines() if line.strip()]
    return [row for row in rows if row["gold"]["expected_kb_ids"]]


def run_eval_ablation(pool: Any) -> dict[str, Any]:
    rows = _eval_rows()
    results: dict[str, Any] = {}
    with pool.connection() as conn:
        for name, search in VARIANTS.items():
            hits = {1: 0, 3: 0, 5: 0}
            reciprocal = 0.0
            misses: list[dict[str, Any]] = []
            for row in rows:
                ticket = row["ticket"]
                ranked = _unique_ids(search(conn, ticket["subject"], ticket["body"]))[:TOP_K]
                expected = set(row["gold"]["expected_kb_ids"])
                rank, hit, rr = _hit_metrics(ranked, expected)
                reciprocal += rr
                if hit:
                    for k in hits:
                        if rank <= k:
                            hits[k] += 1
                else:
                    misses.append({"eval_id": row["id"], "expected": sorted(expected),
                                   "got": ranked[:TOP_K]})
            n = len(rows) or 1
            results[name] = {
                "n": len(rows),
                "hit@1": round(hits[1] / n, 4),
                "hit@3": round(hits[3] / n, 4),
                "hit@5": round(hits[5] / n, 4),
                "mrr": round(reciprocal / n, 4),
                "misses": misses,
            }
    return results


def run_tau_sweep(pool: Any) -> dict[str, Any]:
    """Choose tau on dev only (kb_covered labels), then report the full curve."""
    rows = [json.loads(line) for line in DEV.read_text().splitlines() if line.strip()]
    scored: list[tuple[float, bool]] = []
    with pool.connection() as conn:
        for row in rows:
            chunks = hybrid_search(conn, f"{row['subject']}\n{row['body']}", TOP_K)
            best = max((chunk.rrf_score for chunk in chunks), default=0.0)
            scored.append((best, bool(row["kb_covered"])))

    curve = []
    for tau in TAU_GRID:
        true_positive = sum(1 for score, covered in scored if score >= tau and covered)
        false_positive = sum(1 for score, covered in scored if score >= tau and not covered)
        false_negative = sum(1 for score, covered in scored if score < tau and covered)
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        curve.append({
            "tau": tau,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "escalations": false_positive + false_negative,
        })
    best = max(curve, key=lambda entry: (entry["f1"], entry["precision"]))
    return {"population": len(scored), "curve": curve, "best": best}


def render(payload: dict[str, Any]) -> str:
    lines = [
        "# Retrieval ablation",
        "",
        (
            f"Eval rows with gold KB ids: {next(iter(payload['retrieval'].values()))['n']} "
            "(hit rate is measured on this split only; tuning never touches it)."
        ),
        "",
        "| variant | hit@1 | hit@3 | hit@5 | MRR |",
        "|---|---|---|---|---|",
    ]
    for name, stats in payload["retrieval"].items():
        lines.append(
            f"| {name} | {stats['hit@1']} | {stats['hit@3']} | {stats['hit@5']} | {stats['mrr']} |"
        )
    lines += [
        "",
        ("`hybrid+rerank` is a deterministic lexical tie-break (`rrf * (1 + title overlap)`) "
         "run by this script only; the agent keeps plain hybrid retrieval. It is an "
         "experiment, not a production change."),
        "",
        "## Threshold sweep (dev only)",
        "",
        f"Dev rows: {payload['sweep']['population']}. Positive class = `kb_covered: true`.",
        "",
        "| tau | precision | recall | F1 | errors |",
        "|---|---|---|---|---|",
    ]
    for entry in payload["sweep"]["curve"]:
        lines.append(
            f"| {entry['tau']} | {entry['precision']} | {entry['recall']} | "
            f"{entry['f1']} | {entry['escalations']} |"
        )
    best = payload["sweep"]["best"]
    lines += [
        "",
        (f"Best F1 on dev: tau={best['tau']} (precision {best['precision']}, "
         f"recall {best['recall']}). The shipped default is "
         f"{payload['current_threshold']}; changing it is a prompt/threshold decision that "
         "must be made on dev, not on the eval report."),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieval ablation + threshold sweep")
    parser.add_argument("--db-url", default="postgresql://nimbus:nimbus@localhost:5433/nimbus_eval")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    from app.config import settings

    warm_encoder()
    pool = create_pool(args.db_url)
    pool.open(wait=True)
    try:
        with pool.connection() as conn:
            chunks = conn.execute("SELECT count(*) AS n FROM kb_chunks").fetchone()["n"]
        if not chunks:
            raise SystemExit(f"{args.db_url} has no KB chunks; run the eval harness first")
        payload = {
            "retrieval": run_eval_ablation(pool),
            "sweep": run_tau_sweep(pool),
            "current_threshold": settings.grounding_threshold,
        }
    finally:
        pool.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(payload, indent=2))
    markdown = render(payload)
    out.with_suffix(".md").write_text(markdown + "\n")
    print(markdown)
    print(f"\nwritten: {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
