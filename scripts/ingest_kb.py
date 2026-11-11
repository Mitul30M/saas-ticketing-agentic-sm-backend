"""Parse, chunk, embed, and idempotently upsert the Nimbus knowledge base."""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sentence_transformers import SentenceTransformer

from app.config import settings
from app.db import connect

BASE_DIR = Path(__file__).resolve().parent.parent
KB_DIR = BASE_DIR / "data" / "kb"
WORD_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class Article:
    article_id: str
    title: str
    category: str
    updated: str
    body: str
    content_hash: str


@dataclass(frozen=True)
class Chunk:
    index: int
    heading_path: tuple[str, ...]
    text: str


def parse_article(path: Path, model_name: str) -> Article:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"{path}: missing YAML front matter")
    _, metadata_text, body = raw.split("---\n", 2)
    metadata = yaml.safe_load(metadata_text)
    if not isinstance(metadata, dict):
        raise TypeError(f"{path}: front matter must be a mapping")
    required = ("id", "title", "category", "updated")
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise ValueError(f"{path}: missing front matter fields: {', '.join(missing)}")
    if not body.strip():
        raise ValueError(f"{path}: article body is empty")
    digest = hashlib.sha256(f"{model_name}\0{raw}".encode()).hexdigest()
    return Article(
        article_id=str(metadata["id"]),
        title=str(metadata["title"]),
        category=str(metadata["category"]),
        updated=str(metadata["updated"]),
        body=body.strip(),
        content_hash=digest,
    )


def split_sections(body: str) -> list[tuple[tuple[str, ...], str]]:
    headings: list[str] = []
    sections: list[tuple[tuple[str, ...], str]] = []
    paragraphs: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            if paragraphs:
                sections.append((tuple(headings), "\n".join(paragraphs).strip()))
                paragraphs = []
            level = len(match.group(1))
            headings = headings[: level - 1] + [match.group(2)]
        elif line.strip():
            paragraphs.append(line.strip())
    if paragraphs:
        sections.append((tuple(headings), "\n".join(paragraphs).strip()))
    return sections


def chunk_article(article: Article, target_words: int = 280, overlap_words: int = 42) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading_path, section in split_sections(article.body):
        words = WORD_RE.findall(section)
        if not words:
            continue
        start = 0
        while start < len(words):
            end = min(start + target_words, len(words))
            chunks.append(
                Chunk(
                    index=len(chunks),
                    heading_path=heading_path,
                    text=" ".join(words[start:end]),
                )
            )
            if end == len(words):
                break
            start = end - min(overlap_words, end - start)
    return chunks


def vector_literal(values: Any) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


def load_articles(kb_dir: Path, model_name: str = settings.embed_model) -> list[Article]:
    articles = [parse_article(path, model_name) for path in sorted(kb_dir.glob("KB-*.md"))]
    if not articles:
        raise ValueError(f"no KB articles found in {kb_dir}")
    ids = [article.article_id for article in articles]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate KB article IDs")
    return articles


def ingest(kb_dir: Path, model_name: str) -> tuple[int, int]:
    articles = load_articles(kb_dir, model_name)
    model = SentenceTransformer(model_name)
    conn = connect(settings.database_url)
    try:
        article_count = 0
        chunk_count = 0
        article_ids = {article.article_id for article in articles}
        if article_ids:
            conn.execute(
                "DELETE FROM kb_articles WHERE id <> ALL(%s)",
                (list(article_ids),),
            )
        for article in articles:
            existing = conn.execute(
                "SELECT content_hash FROM kb_articles WHERE id = %s",
                (article.article_id,),
            ).fetchone()
            if existing and existing["content_hash"] == article.content_hash:
                continue
            chunks = chunk_article(article)
            if not chunks:
                raise ValueError(f"{article.article_id}: no chunks generated")
            texts = [chunk_text(article, chunk) for chunk in chunks]
            embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO kb_articles (id, title, category, updated, body, content_hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        category = EXCLUDED.category,
                        updated = EXCLUDED.updated,
                        body = EXCLUDED.body,
                        content_hash = EXCLUDED.content_hash,
                        updated_at = now()
                    """,
                    (
                        article.article_id,
                        article.title,
                        article.category,
                        article.updated,
                        article.body,
                        article.content_hash,
                    ),
                )
                conn.execute("DELETE FROM kb_chunks WHERE kb_id = %s", (article.article_id,))
                for chunk, embedding in zip(chunks, embeddings, strict=True):
                    conn.execute(
                        """
                        INSERT INTO kb_chunks
                            (kb_id, chunk_idx, title, heading_path, text, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            article.article_id,
                            chunk.index,
                            article.title,
                            list(chunk.heading_path),
                            chunk_text(article, chunk),
                            vector_literal(embedding),
                        ),
                    )
            article_count += 1
            chunk_count += len(chunks)
    finally:
        conn.close()
    return article_count, chunk_count


def chunk_text(article: Article, chunk: Chunk) -> str:
    heading = " / ".join(chunk.heading_path)
    prefix = f"{article.title} — {heading}" if heading else article.title
    return f"{prefix}\n{chunk.text}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-dir", type=Path, default=KB_DIR)
    parser.add_argument("--model", default=settings.embed_model)
    args = parser.parse_args()
    articles, chunks = ingest(args.kb_dir, args.model)
    print(f"Ingested {articles} articles and {chunks} chunks")


if __name__ == "__main__":
    main()
