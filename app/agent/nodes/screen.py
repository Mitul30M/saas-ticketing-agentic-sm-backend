"""Injection screening node: heuristics may only FLAG and tighten behaviour —
they never change the route, never loosen permissions, and never leak attacker
text into prompts or traces (the matched text stays out; only flag names and
positions are recorded)."""

from __future__ import annotations

import re
from typing import Any

from app.agent.nodes.common import next_step, trace_node
from app.agent.state import AgentState

# Static patterns; the matched TEXT is never stored, only the flag name.
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction_override", re.compile(
        r"ignore (?:all |any |your |my )?(?:previous |prior |above |earlier |current )?"
        r"(?:instructions|rules|prompts|directions)",
        re.IGNORECASE)),
    ("instruction_override", re.compile(
        r"disregard (?:all )?(?:previous|prior|above|your) (?:instructions|rules|prompts)",
        re.IGNORECASE)),
    ("system_prompt_marker", re.compile(r"\bSYSTEM\s*:|\bASSISTANT\s*:|<<</?", re.IGNORECASE)),
    ("role_injection", re.compile(
        r"\byou are now\b|\bact as\b|\bpretend (?:to be|you are)\b|\bdeveloper mode\b",
        re.IGNORECASE)),
    ("identity_claim", re.compile(
        r"\bi am (?:customer|user) |\bthis is (?:customer|user) |my customer id is ",
        re.IGNORECASE)),
    ("tool_probe", re.compile(
        r"\brun (?:sql|a query)\b|\bexecute (?:sql|the query)\b|\bdelete from\b|\bdrop table\b",
        re.IGNORECASE)),
]


def make_screen_node(pool: Any):
    def screen(state: AgentState) -> dict[str, Any]:
        text = f"{state.get('subject', '')}\n{state.get('body', '')}"
        flags = sorted({name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)})
        ticket_id = str(state.get("ticket_id") or "")
        trace_id = str(state.get("trace_id") or "")
        if flags:
            with pool.connection() as conn:
                conn.execute(
                    "UPDATE tickets SET injection_suspected = TRUE WHERE id = %s",
                    (ticket_id,),
                )
            trace_node(
                pool, ticket_id, trace_id, "injection_screen", "screen",
                {"flag_count": len(flags)}, {"flags": flags, "action": "flagged_only"},
            )
        return {"flags": flags, "step_count": next_step(state)}

    return screen
