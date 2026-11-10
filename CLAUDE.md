# CLAUDE.md

@AGENTS.md

## Claude Code specifics
- `AGENTS.md` (imported above) is the authoritative rule set; `PLAN.md` is the project plan and phase checklist. Read both at the start of each session.
- Start each session by stating which **Phase** and **checklist item** you are working on, and finish by listing what you verified (commands run + results).
- Use plan mode for anything touching the approval gate, executor, refund state machine, idempotency, or grounding rules; show the plan before editing.
- Prefer running `make test` / `make lint` yourself over asking the owner to run them.
- Keep changes small and explain them in plain language — the owner must be able to defend every line in a live interview.
