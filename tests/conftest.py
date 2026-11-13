"""Suite-wide settings.

The test suite must not depend on the developer's `.env` or on a live vendor.
`LLM_PROVIDER=groq` in `.env` (the setting used for eval runs and demos) made
`build_graph(pool)` pick the real provider, so 18 integration tests either
called Groq or failed on a non-stub draft. Pin the stub for the session;
a test that genuinely needs another provider must construct it explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config import settings


@pytest.fixture(scope="session", autouse=True)
def _pin_llm_provider_to_stub() -> Iterator[None]:
    original = settings.llm_provider
    settings.llm_provider = "stub"
    yield
    settings.llm_provider = original
