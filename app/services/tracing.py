"""Cost-aware trace helper for model calls."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from app import db

PRICING_FILE = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"


@lru_cache(maxsize=1)
def pricing() -> dict[str, dict[str, float]]:
    data = yaml.safe_load(PRICING_FILE.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def cost_usd(provider: str, tokens_in: int, tokens_out: int, cache_hit: bool) -> float:
    if cache_hit:
        return 0.0
    rates = pricing().get(provider, {})
    return (
        tokens_in * float(rates.get("input_per_million", 0)) / 1_000_000
        + tokens_out * float(rates.get("output_per_million", 0)) / 1_000_000
    )


def trace_llm(
    pool: Any, *, ticket_id: str, trace_id: str, node: str, input_: dict[str, Any],
    output: Any, tokens_in: int, tokens_out: int, duration_ms: int,
    cache_hit: bool, provider: str,
) -> None:
    with pool.connection() as conn:
        db.trace(
            conn, ticket_id, trace_id, node, "llm", input_, output,
            duration_ms=duration_ms, tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost_usd(provider, tokens_in, tokens_out, cache_hit),
            cache_hit=cache_hit,
        )
