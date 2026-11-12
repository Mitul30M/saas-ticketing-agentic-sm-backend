"""Service configuration from environment (.env supported)."""

from __future__ import annotations

from datetime import datetime

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_port: int = 8000
    log_level: str = "info"

    database_url: str = "postgresql://nimbus:nimbus@localhost:5433/nimbus"

    # LLM: provider "groq" via ChatGroq (reference pattern) or "stub" = deterministic
    # no-LLM mode for plumbing/CI. Never call vendor SDKs outside this layer.
    llm_provider: str = "stub"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    llm_timeout_s: float = 25.0
    # Retry attempts per LLM call, not a per-ticket call budget.
    llm_max_calls: int = 2

    as_of: datetime = datetime.fromisoformat("2026-11-10T12:00:00+00:00")

    # Embeddings: local open model, no paid API needed.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    # Cross-encoder rerank: OFF unless RETRIEVAL_RERANK_MODEL is set (PLAN 4.4).
    rerank_model: str = ""

    # Agent guards / business rules — never hard-code these in code.
    agent_token: str = ""
    step_limit: int = 8
    tool_timeout_s: float = 8.0
    refund_window_days: int = 30

    # Grounding gate (PLAN 4.5): the best RRF fused score must reach this
    # threshold before a draft is attempted. Calibrated on data/dev ONLY;
    # RRF tops out near 2/(k+1) ~= 0.033 for k=60.
    grounding_threshold: float = 0.02
    retrieval_top_k: int = 5


settings = Settings()
