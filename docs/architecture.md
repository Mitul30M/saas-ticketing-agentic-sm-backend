# Architecture — agent graph

Regenerate with `uv run python scripts/gen_diagram.py` (keep in sync with `app/agent/graph.py`).

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	intake_validate(intake_validate)
	injection_screen(injection_screen)
	triage(triage)
	retrieve(retrieve)
	grounding_check(grounding_check)
	draft_reply(draft_reply)
	approval_gate(approval_gate)
	execute_approved(execute_approved)
	escalate(escalate)
	finalize(finalize)
	__end__([<p>__end__</p>]):::last
	__start__ --> intake_validate;
	approval_gate -.-> escalate;
	approval_gate -.-> execute_approved;
	draft_reply -.-> approval_gate;
	draft_reply -.-> escalate;
	escalate --> finalize;
	execute_approved --> finalize;
	grounding_check -.-> draft_reply;
	grounding_check -.-> escalate;
	injection_screen -.-> escalate;
	injection_screen -.-> triage;
	intake_validate -.-> escalate;
	intake_validate -.-> injection_screen;
	retrieve -.-> escalate;
	retrieve -.-> grounding_check;
	triage -.-> escalate;
	triage -.-> retrieve;
	finalize --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
