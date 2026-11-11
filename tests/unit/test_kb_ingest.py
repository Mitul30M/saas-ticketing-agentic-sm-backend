"""Pure KB parser/chunker tests."""

from pathlib import Path

from scripts.ingest_kb import chunk_article, chunk_text, load_articles, parse_article

KB_DIR = Path("data/kb")


def test_loads_all_articles_with_model_specific_hash() -> None:
    first = load_articles(KB_DIR, "model-a")
    second = load_articles(KB_DIR, "model-b")

    assert len(first) == 30
    assert first[0].content_hash != second[0].content_hash


def test_chunk_text_contains_title_and_heading_for_search() -> None:
    article = parse_article(KB_DIR / "KB-006.md", "model-a")
    chunks = chunk_article(article)

    assert chunk_text(article, chunks[0]).startswith("Nimbus refund policy —")
    assert any("Refunds are proposals" in chunk_text(article, chunk) for chunk in chunks)
