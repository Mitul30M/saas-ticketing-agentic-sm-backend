"""Explicit orchestration stages with no model or database side effects."""

from __future__ import annotations

from typing import Any

from app.agent.state import AgentState


def make_plan_node():
    def plan(state: AgentState) -> dict[str, Any]:
        return {"plan": "retrieve_and_ground"}

    return plan


def make_tool_executor_node():
    def tool_executor(state: AgentState) -> dict[str, Any]:
        return {}

    return tool_executor


def make_propose_actions_node():
    def propose_actions(state: AgentState) -> dict[str, Any]:
        return {"action_proposed": False}

    return propose_actions


def make_failure_handler_node():
    def failure_handler(state: AgentState) -> dict[str, Any]:
        escalation = dict(state.get("escalation") or {})
        escalation.setdefault("reason_code", "INTERNAL_ERROR")
        escalation.setdefault("detail", "agent failure requires human review")
        return {"outcome": "escalated", "escalation": escalation}

    return failure_handler
