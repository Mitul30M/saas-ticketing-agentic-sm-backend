"""Typed HTTP request and response models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TicketCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    from_email: str = Field(min_length=3, max_length=320)
    from_name: str | None = Field(default=None, max_length=200)


class TicketCreated(BaseModel):
    ticket_id: str
    duplicate: bool


class DecisionBody(BaseModel):
    decision: str = Field(pattern=r"^(approve|reject|edit)$")
    note: str | None = Field(default=None, max_length=2000)
    edited_text: str | None = Field(default=None, max_length=4000)


class DecisionAccepted(BaseModel):
    approval_id: str
    status: str = "accepted"


class TicketView(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    subject: str
    body: str
    category: str | None = None
    priority: str | None = None
    sentiment: str | None = None
    queue: str | None = None
    escalated: bool
    draft_reply: str | None = None
    draft_citations: list[str] | None = None
