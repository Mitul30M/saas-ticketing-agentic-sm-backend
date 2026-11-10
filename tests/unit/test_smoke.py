"""Unit smoke tests: app imports cleanly and expected structures exist (no DB)."""

from __future__ import annotations


def test_app_routes_registered() -> None:
    from app.main import app

    paths = {r.path for r in app.routes}
    assert "/healthz" in paths


def test_settings_defaults() -> None:
    from app.config import settings

    assert settings.step_limit >= 1
    assert settings.tool_timeout_s > 0
    assert settings.refund_window_days > 0
    assert settings.llm_provider in {"groq", "stub"}


def test_migrations_dir_has_expected_files() -> None:
    from app.db import MIGRATIONS_DIR

    names = {p.name for p in MIGRATIONS_DIR.glob("*.sql")}
    assert {
        "001_extensions.sql",
        "002_core.sql",
        "003_support.sql",
        "004_platform.sql",
        "005_kb.sql",
    } <= names
