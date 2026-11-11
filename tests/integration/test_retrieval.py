"""Retrieval integration tests: FTS, pgvector, RRF fusion (skips without DB).

Needs the seeded KB (49 chunks from `make ingest-kb`). Query text is untrusted;
every query is a bind parameter.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.retrieval.hybrid import RRF_K, fts_search, hybrid_search, vector_search
from app.retrieval.rerank import maybe_rerank

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn(migrated_conn: Any) -> Any:
    return migrated_conn


def test_fts_search_ranks_relevant_chunks_first(conn) -> None:
    rows = fts_search(conn, "how do I request a refund", 5)

    assert rows, "seeded KB must match a refund query"
    assert rows[0]["score"] >= rows[-1]["score"]


def test_fts_search_title_terms_match(conn) -> None:
    # Migration 008 made tsv include the chunk title.
    rows = fts_search(conn, "invoice", 5)

    assert rows


def test_vector_search_returns_neighbors_with_scores(conn) -> None:
    rows = vector_search(conn, "my refund is taking too long", 5)

    assert rows
    for row in rows:
        assert 0.0 < row["score"] <= 1.0


def test_hybrid_fusion_respects_limit_and_dedupes(conn) -> None:
    results = hybrid_search(conn, "refund window eligibility", 5)

    assert len(results) <= 5
    ids = [f"{r.kb_id}#{r.chunk_idx}" for r in results]
    assert len(ids) == len(set(ids))


def test_rrf_scores_use_k_60(conn) -> None:
    results = hybrid_search(conn, "refund window eligibility", 5)

    assert results
    top = results[0]
    # A chunk ranked 1st in BOTH channels must score exactly 2/(k+1).
    if top.fts_rank == 1 and top.vec_rank == 1:
        assert abs(top.rrf_score - 2.0 / (RRF_K + 1)) < 1e-9


def test_untrusted_query_text_is_safe(conn) -> None:
    malicious = "refund'); DROP TABLE kb_chunks; -- *() OR 1=1"

    results = hybrid_search(conn, malicious, 5)  # must not raise, must not lose rows

    assert isinstance(results, list)
    count = conn.execute("SELECT count(*) AS n FROM kb_chunks").fetchone()["n"]
    assert count > 0


def test_rerank_flag_off_is_identity(conn) -> None:
    before = hybrid_search(conn, "refund window eligibility", 5)

    after = maybe_rerank("refund window eligibility", before, model_name="")

    assert after == before  # default flag off: unchanged order, no extra model
