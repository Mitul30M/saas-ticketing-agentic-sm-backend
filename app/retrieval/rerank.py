"""Optional cross-encoder rerank, feature-flagged OFF by default (PLAN 4.4).

Enable by setting RETRIEVAL_RERANK_MODEL to a cross-encoder model name.
Headline retrieval numbers in evals must state whether rerank was on.
"""

from __future__ import annotations

from typing import Any

from app.retrieval.hybrid import RetrievedChunk

_cross_encoder: Any = None


def maybe_rerank(query: str, chunks: list[RetrievedChunk], model_name: str = "") -> list[RetrievedChunk]:
    """Identity when the flag is off (default). On: CrossEncoder scores
    (query, chunk) pairs and re-sorts descending. Order may only change
    WHICH evidence surfaces first — never permissions or policy."""
    if not model_name or not chunks:
        return chunks
    global _cross_encoder
    if _cross_encoder is None:
        from sentence_transformers import CrossEncoder

        _cross_encoder = CrossEncoder(model_name)
    scores = _cross_encoder.predict(
        [(query, chunk.text) for chunk in chunks], show_progress_bar=False
    )
    return [chunk for _, chunk in sorted(zip(scores, chunks, strict=True), key=lambda pair: pair[0], reverse=True)]
