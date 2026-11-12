"""Reproducible grounding evidence for the dev split (P4-0.3).

    make grounding-report
    (or) uv run python -m scripts.grounding_report

For every ticket in data/dev/dev_tickets.jsonl, runs the same hybrid retrieval
the agent uses and writes eval/results/grounding_dev.csv with the retrieval
gate evidence: kb_covered (hand label from data/kb/COVERAGE.md), best RRF
score, the top chunk, per-channel ranks and whether both channels agreed.

With LLM_PROVIDER=groq (never with the stub), each ticket also gets the real
support-check verdict and reason, so the not-in-KB behaviour can be measured
against the same model the eval will use. Needs the seeded database running
(make up && make migrate && make ingest-kb)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from psycopg.rows import dict_row

from app.agent.nodes.grounding import (
    SupportCheckOutput,
    _system_prompt,
    _user_content,
)
from app.config import settings
from app.llm.base import LLMClient
from app.retrieval.hybrid import hybrid_search

DEV_FILE = Path("data/dev/dev_tickets.jsonl")
OUT_FILE = Path("eval/results/grounding_dev.csv")

BASE_COLUMNS = [
    "external_ref",
    "kb_covered",
    "best_rrf",
    "top_kb_id",
    "fts_rank",
    "vec_rank",
    "channels_agreed",
    "supported",
    "reason",
]


def main() -> None:
    import psycopg

    with psycopg.connect(
        settings.database_url, row_factory=dict_row, autocommit=True
    ) as conn:
        client: LLMClient | None = None
        if settings.llm_provider != "stub":
            client = LLMClient(pool=None)
        rows = [json.loads(line) for line in DEV_FILE.read_text().splitlines() if line.strip()]
        OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with OUT_FILE.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=BASE_COLUMNS)
            writer.writeheader()
            for ticket in rows:
                text = f"{ticket['subject']}\n{ticket['body']}"
                chunks = hybrid_search(conn, text, settings.retrieval_top_k)
                top = chunks[0] if chunks else None
                row = {
                    "external_ref": ticket["external_ref"],
                    "kb_covered": ticket.get("kb_covered", ""),
                    "best_rrf": f"{max((c.rrf_score for c in chunks), default=0.0):.4f}",
                    "top_kb_id": top.kb_id if top else "",
                    "fts_rank": top.fts_rank if top and top.fts_rank else "",
                    "vec_rank": top.vec_rank if top and top.vec_rank else "",
                    "channels_agreed": bool(top and top.fts_rank and top.vec_rank),
                    "supported": "",
                    "reason": "",
                }
                if client is not None and chunks:
                    response = client.generate(
                        [
                            {"role": "system", "content": _system_prompt()},
                            {"role": "user", "content": _user_content(
                                ticket["subject"], ticket["body"], [c.__dict__ for c in chunks]
                            )},
                        ],
                        schema=SupportCheckOutput,
                    )
                    verdict = response.structured
                    if verdict is not None:
                        row["supported"] = verdict.supported
                        row["reason"] = verdict.reason[:300]
                writer.writerow(row)
    print(f"wrote {OUT_FILE} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
