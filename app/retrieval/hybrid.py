"""Hybrid retrieval over kb_chunks: Postgres FTS + pgvector cosine, fused with
Reciprocal Rank Fusion (k=60, PLAN 4.4). All SQL is parameterised; the query
text is untrusted and only ever a bind parameter (websearch_to_tsquery is
injection-safe by design).
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Any

RRF_K = 60

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class RetrievedChunk:
    kb_id: str
    chunk_idx: int
    title: str
    updated: str
    heading_path: list[str]
    text: str
    rrf_score: float
    fts_rank: int | None  # 1-based position in the FTS list; None = not matched
    vec_rank: int | None
    fts_score: float | None
    vec_score: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_encoder: Any = None


def _encoder_instance() -> Any:
    global _encoder
    if _encoder is None:
        from sentence_transformers import SentenceTransformer

        from app.config import settings
        _encoder = SentenceTransformer(settings.embed_model)
    return _encoder


def embed_query(text: str) -> list[float]:
    """Local open embedding model (same model as ingest, PLAN 4.4)."""
    values = _encoder_instance().encode(text, normalize_embeddings=True)
    return [float(value) for value in values]


def warm_encoder() -> float:
    """Load the embedding model and run one encode; returns elapsed seconds.

    Called once by the API lifespan and by the worker before it polls, because a
    cold load (torch import + weight init + any model download) can take longer
    than TOOL_TIMEOUT_S. Without this the first kb_search of a fresh deployment
    timed out and escalated the ticket. Keeps the module-level instance, so the
    warm-up and the real queries share one model.
    """
    started = time.perf_counter()
    embed_query("warmup")
    return time.perf_counter() - started


def vector_literal(values: Any) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


def _fts_query(query: str) -> str:
    """OR-combine sanitized tokens. websearch_to_tsquery defaults to AND, so a
    full natural sentence ("How does the refund policy work?") matched nothing;
    ticket text is untrusted, so tokens are reduced to [A-Za-z0-9_]+ before they
    ever reach SQL (still passed as a bind parameter)."""
    words = _WORD_RE.findall(query)[:64]
    return " or ".join(words)


def fts_search(conn: Any, query: str, limit: int) -> list[dict[str, Any]]:
    fts = _fts_query(query)
    if not fts:
        return []
    rows = conn.execute(
        "SELECT c.id, c.kb_id, c.chunk_idx, c.title, a.updated, c.heading_path, c.text, "
        "a.updated AS article_updated, "
        "ts_rank(tsv, websearch_to_tsquery('english', %s)) AS score "
        "FROM kb_chunks c JOIN kb_articles a ON a.id = c.kb_id "
        "WHERE tsv @@ websearch_to_tsquery('english', %s) "
        "ORDER BY score DESC LIMIT %s",
        (fts, fts, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def vector_search(conn: Any, query: str, limit: int) -> list[dict[str, Any]]:
    embedding = vector_literal(embed_query(query))
    rows = conn.execute(
        "SELECT c.id, c.kb_id, c.chunk_idx, c.title, a.updated, c.heading_path, c.text, "
        "a.updated AS article_updated, "
        "1 - (embedding <=> %s::vector) AS score "
        "FROM kb_chunks c JOIN kb_articles a ON a.id = c.kb_id "
        "ORDER BY embedding <=> %s::vector LIMIT %s",
        (embedding, embedding, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _fuse(
    fts_rows: list[dict[str, Any]], vec_rows: list[dict[str, Any]], limit: int
) -> list[RetrievedChunk]:
    """RRF: chunk score = sum over lists of 1 / (k + 1-based rank)."""
    fused: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(fts_rows, start=1):
        entry = fused.setdefault(
            str(row["id"]),
            {"row": row, "rrf": 0.0, "fts_rank": None, "vec_rank": None},
        )
        entry["rrf"] += 1.0 / (RRF_K + rank)
        entry["fts_rank"] = rank
    for rank, row in enumerate(vec_rows, start=1):
        entry = fused.setdefault(
            str(row["id"]),
            {"row": row, "rrf": 0.0, "fts_rank": None, "vec_rank": None},
        )
        entry["rrf"] += 1.0 / (RRF_K + rank)
        entry["vec_rank"] = rank
    ranked = sorted(fused.values(), key=lambda entry: entry["rrf"], reverse=True)[:limit]
    results: list[RetrievedChunk] = []
    for entry in ranked:
        row = entry["row"]
        results.append(
            RetrievedChunk(
                kb_id=str(row["kb_id"]),
                chunk_idx=int(row["chunk_idx"]),
                title=str(row["title"]),
                updated=str(row.get("article_updated") or row.get("updated")),
                heading_path=list(row["heading_path"] or []),
                text=str(row["text"]),
                rrf_score=float(entry["rrf"]),
                fts_rank=entry["fts_rank"],
                vec_rank=entry["vec_rank"],
                fts_score=float(row["score"]) if "score" in row and entry["fts_rank"] else None,
                vec_score=float(row["score"]) if entry["vec_rank"] else None,
            )
        )
    return results


def hybrid_search(
    conn: Any, query: str, limit: int = 5, fts_limit: int | None = None, vec_limit: int | None = None
) -> list[RetrievedChunk]:
    """Retrieve with both channels and fuse. `limit*2` candidates per channel."""
    fts_limit = fts_limit or limit * 2
    vec_limit = vec_limit or limit * 2
    return _fuse(fts_search(conn, query, fts_limit), vector_search(conn, query, vec_limit), limit)
