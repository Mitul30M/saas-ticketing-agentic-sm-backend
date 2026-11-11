"""Validated input/output contracts for agent tools."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator


class ToolErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    BUSINESS_RULE = "business_rule"
    DUPLICATE = "duplicate"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


class ToolError(BaseModel):
    code: ToolErrorCode
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None


class TicketContext(BaseModel):
    """from_email in arguments is NEVER trusted (invariant 11): identity is
    resolved server-side from tickets.from_email. Kept optional and unused."""

    ticket_id: UUID
    from_email: str | None = Field(default=None, min_length=3, max_length=320)


class LookupCustomerInput(BaseModel):
    """Identity is resolved from tickets.from_email in the DB (invariant 11);
    a from_email CLAIMED in the arguments is ignored."""

    ticket_id: UUID
    from_email: str | None = Field(default=None, min_length=3, max_length=320)


class KBSearchInput(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)


class CustomerLookupInput(TicketContext):
    pass


class CustomerLookupOutput(BaseModel):
    customer_id: UUID
    email: str
    first_name: str
    last_name: str
    status: str


class CustomerResourceInput(TicketContext):
    customer_id: UUID


class InvoiceInput(CustomerResourceInput):
    invoice_id: str = Field(pattern=r"^INV-\d{4,8}-[A-Z0-9]+$")


class RefundProposalInput(CustomerResourceInput):
    invoice_id: str = Field(pattern=r"^INV-\d{4,8}-[A-Z0-9]+$")
    amount_cents: PositiveInt
    reason: str = Field(min_length=3, max_length=1000)
    kb_ids: list[str] = Field(min_length=1, max_length=10)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("kb_ids")
    @classmethod
    def validate_kb_ids(cls, values: list[str]) -> list[str]:
        if any(not value.startswith("KB-") for value in values):
            raise ValueError("kb_ids must contain KB article IDs")
        return values


class PlanChangeProposalInput(CustomerResourceInput):
    to_plan_id: str = Field(min_length=1, max_length=100)
    effective: str = Field(default="next_cycle", pattern=r"^(now|next_cycle)$")
    reason: str = Field(min_length=3, max_length=1000)
    kb_ids: list[str] = Field(min_length=1, max_length=10)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("kb_ids")
    @classmethod
    def validate_kb_ids(cls, values: list[str]) -> list[str]:
        if any(not value.startswith("KB-") for value in values):
            raise ValueError("kb_ids must contain KB article IDs")
        return values


class EscalationInput(BaseModel):
    ticket_id: UUID
    reason_code: str = Field(min_length=1, max_length=100)
    detail: str = Field(min_length=1, max_length=2000)


class ToolContext(BaseModel):
    trace_id: str = Field(min_length=1, max_length=200)
    timeout_s: float = Field(default=8, gt=0, le=60)
