"""Tool registry: the only path from the graph to any capability.

Contract (tests/unit/test_tool_registry.py):
- call() NEVER raises; every failure is a typed ToolResult(ok=False, error).
- Input validated with the tool's Pydantic model BEFORE the handler runs.
- A hung handler is cut off after ctx.timeout_s -> TIMEOUT (retryable).
- Handlers raise ToolFailure for expected business failures (passed through).
- Unexpected exceptions become a generic INTERNAL error (no internals leak).
- Exactly ONE trace row per call, written by the registry only, so a
  handler abandoned after a timeout can never add a second row.

Real tools are wired in build_registry(pool); handlers live in handlers.py.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ValidationError

from app import db
from app.tools.schemas import ToolContext, ToolError, ToolErrorCode, ToolResult

# Module-level executor: abandoned (timed-out) handlers finish in the
# background; the registry never waits for them and never traces twice.
EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], dict[str, Any]]


class ToolFailure(RuntimeError):
    """Raised by handlers for EXPECTED business failures; passed through typed."""

    def __init__(self, code: ToolErrorCode, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _result_from_failure(code: ToolErrorCode, message: str, retryable: bool) -> ToolResult:
    return ToolResult(ok=False, data=None, error=ToolError(code=code, message=message, retryable=retryable))


def _validation_message(exc: ValidationError) -> str:
    """Field names + static messages only — never echo rejected VALUES (they
    may be attacker text that must not flow back to the model)."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err.get("loc", ())) or "args"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec], tracer: Callable[..., None]) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._tracer = tracer

    def call(self, name: str, raw_args: Any, *, ticket_id: str, ctx: ToolContext) -> ToolResult:
        started = perf_counter()
        spec = self._specs.get(name)
        if spec is None:
            result = _result_from_failure(
                ToolErrorCode.INVALID_INPUT, f"unknown tool: {name}", retryable=False
            )
            self._trace(name, raw_args, ticket_id, ctx, result, started)
            return result

        if not isinstance(raw_args, dict):
            result = _result_from_failure(
                ToolErrorCode.INVALID_INPUT, "arguments: must be a JSON object", retryable=False
            )
            self._trace(name, raw_args, ticket_id, ctx, result, started)
            return result

        try:
            args = spec.input_model.model_validate(raw_args)
        except ValidationError as exc:
            result = _result_from_failure(
                ToolErrorCode.INVALID_INPUT, _validation_message(exc), retryable=False
            )
            self._trace(name, raw_args, ticket_id, ctx, result, started)
            return result

        try:
            future = EXECUTOR.submit(spec.handler, args)
            data = future.result(timeout=ctx.timeout_s)
        except ToolFailure as failure:
            result = _result_from_failure(failure.code, failure.message, failure.retryable)
        except TimeoutError:  # concurrent.futures.TimeoutError == builtin TimeoutError (py3.11+)
            result = _result_from_failure(
                ToolErrorCode.TIMEOUT, f"tool timed out after {ctx.timeout_s}s", retryable=True
            )
        except Exception:
            result = _result_from_failure(
                ToolErrorCode.INTERNAL, "internal tool error", retryable=True
            )
        else:
            if not isinstance(data, dict):
                result = _result_from_failure(
                    ToolErrorCode.INTERNAL, "internal tool error", retryable=True
                )
            else:
                result = ToolResult(ok=True, data=data, error=None)

        self._trace(name, args, ticket_id, ctx, result, started)
        return result

    def _trace(
        self, name: str, args: Any, ticket_id: str, ctx: ToolContext,
        result: ToolResult, started: float,
    ) -> None:
        duration_ms = int((perf_counter() - started) * 1000)
        error_code = result.error.code.value if result.error else None
        if isinstance(args, dict):
            traced_args: Any = args
        elif isinstance(args, BaseModel):
            traced_args = args.model_dump(mode="json")  # keep proposal evidence queryable
        else:
            traced_args = {"raw": str(args)}
        try:
            self._tracer(
                ticket_id=ticket_id,
                trace_id=ctx.trace_id,
                node=name,
                kind="tool",
                input_=traced_args,
                # On failure keep the typed error, not just the code: the
                # dashboard showed "business_rule" with no hint which rule, and
                # the message is the operator's only clue.
                output=result.data if result.ok else (
                    {"error": result.error.model_dump(mode="json")} if result.error else None
                ),
                status="ok" if result.ok else "error",
                error=error_code,
                duration_ms=duration_ms,
            )
        except Exception:
            LOGGER.exception("tool trace write failed for %s", name)


def _pool_tracer(pool: Any) -> Callable[..., None]:
    def tracer(**kwargs: Any) -> None:
        with pool.connection() as conn:
            db.trace(
                conn,
                kwargs["ticket_id"],
                kwargs["trace_id"],
                kwargs["node"],
                kwargs["kind"],
                kwargs.get("input_"),
                kwargs.get("output"),
                status=kwargs.get("status", "ok"),
                error=kwargs.get("error"),
                duration_ms=kwargs.get("duration_ms"),
            )

    return tracer


def build_registry(pool: Any) -> ToolRegistry:
    """Wire every real tool. Handlers close over the connection pool."""
    from app.tools import handlers

    def wrap(fn: Callable[[Any, Any], dict[str, Any]]) -> Callable[[BaseModel], dict[str, Any]]:
        return lambda args: fn(pool, args)

    specs = [
        ToolSpec(name="lookup_customer",
                 input_model=handlers.LookupCustomerInput, handler=wrap(handlers.lookup_customer)),
        ToolSpec(name="get_invoice",
                 input_model=handlers.InvoiceInput, handler=wrap(handlers.get_invoice)),
        ToolSpec(name="get_subscription",
                 input_model=handlers.CustomerResourceInput, handler=wrap(handlers.get_subscription)),
        ToolSpec(name="kb_search",
                 input_model=handlers.KBSearchInput, handler=wrap(handlers.kb_search)),
        ToolSpec(name="propose_refund",
                 input_model=handlers.RefundProposalInput, handler=wrap(handlers.propose_refund)),
        ToolSpec(name="propose_plan_change",
                 input_model=handlers.PlanChangeProposalInput,
                 handler=wrap(handlers.propose_plan_change)),
        ToolSpec(name="escalate_to_human",
                 input_model=handlers.EscalationInput, handler=wrap(handlers.escalate_to_human)),
    ]
    return ToolRegistry(specs, _pool_tracer(pool))
