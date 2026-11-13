from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from app.domain.enums import CATEGORY_QUEUE, Category, Queue
from app.llm.base import LLMClient, LLMError, parse_structured
from app.llm.cache import cache_key
from app.tools.schemas import RefundProposalInput


class StubOutput(BaseModel):
    status: str
    reasons: dict[str, str]


def test_category_queue_mapping_is_deterministic() -> None:
    assert CATEGORY_QUEUE[Category.REFUND_REQUEST] is Queue.BILLING
    assert CATEGORY_QUEUE[Category.SECURITY_INCIDENT] is Queue.SECURITY


def test_stub_client_can_return_requested_schema() -> None:
    result = LLMClient("stub").generate(
        [{"role": "user", "content": "classify"}],
        schema=StubOutput,
    )
    assert result.structured is not None


def test_structured_response_rejects_invalid_json() -> None:
    with pytest.raises(LLMError, match="invalid JSON"):
        parse_structured("not-json", RefundProposalInput)


def test_refund_input_requires_evidence_and_valid_invoice() -> None:
    with pytest.raises(ValidationError):
        RefundProposalInput(
            ticket_id=uuid4(),
            customer_id=uuid4(),
            invoice_id="invoice-1",
            amount_cents=100,
            reason="duplicate charge",
            kb_ids=[],
            idempotency_key="request-123",
        )


def test_cache_key_is_order_stable() -> None:
    messages = [{"role": "user", "content": "hello"}]
    assert cache_key("stub", "model", messages, {"temperature": 0}) == cache_key(
        "stub", "model", messages, {"temperature": 0}
    )


def test_cache_key_changes_when_request_changes() -> None:
    messages = [{"role": "user", "content": "hello"}]
    base = cache_key("stub", "model", messages, {"temperature": 0})
    assert base != cache_key("stub", "model", messages, {"temperature": 1})
    assert base != cache_key("stub", "model", [{"role": "user", "content": "different"}], {"temperature": 0})


def test_schema_error_names_the_offending_field() -> None:
    """A real model omitted `reason`; the old error hid which field was wrong."""
    from pydantic import BaseModel

    class NeedsReason(BaseModel):
        reason: str

    with pytest.raises(LLMError, match=r"\(reason: Field required\)"):
        parse_structured('{"ok": true}', NeedsReason)


def test_support_check_reason_is_optional() -> None:
    """gpt-oss-120b dropped `reason`; the verdict itself is still usable."""
    from app.agent.nodes.grounding import SupportCheckOutput

    verdict = SupportCheckOutput.model_validate({
        "supported": True,
        "conflicting": False,
        "claims": [{"claim": "Refunds are windowed.", "kb_id": "KB-006"}],
    })
    assert verdict.supported is True
    assert verdict.reason == ""
