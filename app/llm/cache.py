"""Database-backed cache for identical LLM requests."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def cache_key(
    provider: str, model: str, messages: list[dict[str, str]], params: dict[str, Any]
) -> str:
    payload = json.dumps(
        {"provider": provider, "model": model, "messages": messages, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def load_cached(conn: Any, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT response, tokens_in, tokens_out FROM llm_cache WHERE key = %s",
        (key,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE llm_cache SET hit_count = hit_count + 1, last_hit_at = now() WHERE key = %s",
        (key,),
    )
    return dict(row)


def store_cached(
    conn: Any,
    key: str,
    response: dict[str, Any],
    tokens_in: int,
    tokens_out: int,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO llm_cache (key, response, tokens_in, tokens_out, provider, model)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (key) DO NOTHING
        """,
        (key, json.dumps(response), tokens_in, tokens_out, provider, model),
    )
