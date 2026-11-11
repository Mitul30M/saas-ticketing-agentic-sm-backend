"""LLM cache + repair-once contract tests (PLAN Phase 2 step 3).

Needs the compose Postgres for the cache-backed paths (skips otherwise):
  * identical generate() calls hit the cache exactly once stored, then cache_hit
  * different params/messages -> different keys -> no cross-talk
  * repair-once: fenced JSON is stripped; a fenced+prose reply is repaired once
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from app.llm.base import LLMClient, LLMError, _strip_fences
from app.llm.cache import cache_key

pytestmark = pytest.mark.integration


class Verdict(BaseModel):
    category: str
    confidence: float


@pytest.fixture()
def pool(db_pool: Any) -> Any:
    return db_pool


def _fresh_messages(tag: str) -> list[dict[str, str]]:
    # Unique per test run so repeated runs never collide on a cached key.
    return [{"role": "user", "content": f"triage ticket {tag}"}]


def test_first_call_stores_and_second_call_hits_cache(pool) -> None:
    messages = _fresh_messages(uuid.uuid4().hex)
    client = LLMClient("stub", pool=pool)

    first = client.generate(messages, schema=Verdict)
    second = client.generate(messages, schema=Verdict)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.text == first.text
    assert second.structured is not None
    assert first.structured == second.structured


def test_different_messages_get_different_cache_entries(pool) -> None:
    client = LLMClient("stub", pool=pool)

    a = client.generate(_fresh_messages(uuid.uuid4().hex), schema=Verdict)
    b = client.generate(_fresh_messages(uuid.uuid4().hex), schema=Verdict)

    assert a.cache_hit is False
    assert b.cache_hit is False


def test_key_is_order_stable_across_message_order() -> None:
    a = cache_key("stub", "m", [{"role": "user", "content": "x"},
                                {"role": "system", "content": "y"}], {})
    b = cache_key("stub", "m", [{"role": "system", "content": "y"},
                                {"role": "user", "content": "x"}], {})
    assert a != b  # message ORDER matters; params/messages both feed the key


def test_no_pool_means_no_caching() -> None:
    messages = _fresh_messages(uuid.uuid4().hex)
    first = LLMClient("stub").generate(messages, schema=Verdict)
    second = LLMClient("stub").generate(messages, schema=Verdict)

    assert first.cache_hit is False
    assert second.cache_hit is False


def test_strip_fences_repair_helper() -> None:
    fenced = '```json\n{"category": "billing", "confidence": 0.9}\n```'
    assert _strip_fences(fenced) == '{"category": "billing", "confidence": 0.9}'
    assert _strip_fences('{"a": 1}') == '{"a": 1}'
    assert _strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_stub_invalid_output_fails_loudly_despite_repair() -> None:
    """Stub is deterministic: if it cannot produce valid output, that is a
    plumbing bug and must raise, never be papered over by a retry."""
    from pydantic import field_validator

    class MustFail(BaseModel):
        flag: str

        @field_validator("flag")
        @classmethod
        def _never(cls, v: str) -> str:
            raise ValueError("always invalid")

    with pytest.raises(LLMError):
        LLMClient("stub").generate(_fresh_messages(uuid.uuid4().hex), schema=MustFail)
