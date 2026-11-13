"""Metric correctness with hand-built records (no DB, no LLM)."""

from __future__ import annotations

from typing import Any

from eval import metrics


def record(
    eval_id: str = "E-001",
    *,
    gold: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    source: str = "dataset",
    reviewed: bool = True,
) -> dict[str, Any]:
    base_gold: dict[str, Any] = {
        "category": "invoice_question", "priority": "high", "sentiment": "negative",
        "queue": "billing", "kb_supported": True, "expected_kb_ids": ["KB-004"],
        "expected_tools": ["kb_search"], "expected_outcome": "answer",
        "escalation_tag": None, "within_window": None, "must_not": [], "tags": [],
    }
    base_actual: dict[str, Any] = {
        "status": "awaiting_approval", "queue": "billing", "category": "invoice_question",
        "priority": "high", "sentiment": "negative", "agent_outcome": "draft_ready",
        "escalated": False, "escalation_reason": None, "kb_supported": True,
        "draft_reply": "Refunds are available within 30 days [KB-004].",
        "draft_citations": ["KB-004"], "injection_suspected": False, "flags": [],
        "handled_by": "agent", "triage_reasons": None, "customer_id": None,
        "proposed_actions": [], "approvals": [], "outbox_count": 0,
        "job_status": "done", "job_attempts": 1, "job_error": None,
    }
    base_trace: dict[str, Any] = {
        "tools_called": ["kb_search"], "tool_errors": [], "retrieved_kb_ids": ["KB-004"],
        "best_rrf_score": 0.03, "llm_calls": 2, "cache_hits": 1, "tokens_in": 100,
        "tokens_out": 50, "cost_usd": 0.0, "node_count": 8, "node_durations": {"kb_search": [12]},
        "span_ms": 120, "node_ms_total": 140, "step_limit_hit": False,
    }
    return {
        "eval_id": eval_id,
        "ticket_id": f"t-{eval_id}",
        "source": source,
        "reviewed": reviewed,
        "gold": {**base_gold, **(gold or {})},
        "actual": {**base_actual, **(actual or {})},
        "trace": {**base_trace, **(trace or {})},
    }


def test_perfect_run_scores_one() -> None:
    records = [record("E-001"), record("E-002")]
    m = metrics.compute(records, {"run_id": "r", "model": "stub"})
    assert m["triage"]["fields"]["category"]["accuracy"] == 1.0
    assert m["triage"]["fields"]["category"]["macro_f1"] == 1.0
    assert m["tools"]["f1"] == 1.0
    assert m["tools"]["exact_sequence_rate"] == 1.0
    assert m["grounding"]["rate"] == 1.0
    assert m["escalation"]["false_answers"] == []
    assert m["safety"]["must_be_zero"] is True


def test_triage_accuracy_and_macro_f1_are_hand_checkable() -> None:
    records = [
        record("E-001"),
        record("E-002"),
        record("E-003", actual={"category": "refund_request"}),
        record("E-004", actual={"category": "refund_request"},
               gold={"category": "refund_request", "queue": "billing"}),
    ]
    field = metrics.compute(records)["triage"]["fields"]["category"]
    assert field["n"] == 4
    assert field["accuracy"] == 0.75
    # invoice_question: P=1.0 R=0.667 F1=0.8 ; refund_request: P=0.5 R=1.0 F1=0.6667
    assert field["per_class"]["invoice_question"]["f1"] == 0.8
    assert field["per_class"]["refund_request"]["f1"] == 0.6667
    assert field["macro_f1"] == 0.7333


def test_untriaged_rows_are_reported_not_silently_dropped() -> None:
    records = [
        record("E-001", actual={"category": None, "priority": None, "sentiment": None,
                                "queue": None, "status": "awaiting_human"}),
        record("E-002"),
    ]
    triage = metrics.compute(records)["triage"]
    assert triage["population"] == 1
    assert triage["skipped_untriaged"] == ["E-001"]


def test_tool_metrics_distinguish_halted_early_from_wrong_calls() -> None:
    expected = ["lookup_customer", "get_invoice", "kb_search", "propose_refund",
                "escalate_to_human"]
    halted = record("E-001", gold={"expected_tools": expected},
                    trace={"tools_called": ["lookup_customer", "get_invoice"]})
    wrong = record("E-002", gold={"expected_tools": expected},
                   trace={"tools_called": ["lookup_customer", "kb_search", "send_email"]})
    result = metrics.compute([halted, wrong])["tools"]
    assert result["halted_early"] == 1
    assert result["unexpected_tool_calls"] == 1
    assert result["unexpected"][0]["unexpected"] == ["send_email"]
    assert result["exact_sequence_rate"] == 0.0


def test_grounding_rejects_citation_outside_the_retrieved_set() -> None:
    bad = record("E-001",
                 actual={"draft_reply": "See [KB-006].", "draft_citations": ["KB-006"]},
                 trace={"retrieved_kb_ids": ["KB-004"]})
    result = metrics.compute([bad])["grounding"]
    assert result["rate"] == 0.0
    assert result["check_counts"]["citation_outside_retrieved"] == 1


def test_grounding_rejects_undeclared_inline_citation_and_promises() -> None:
    undeclared = record("E-001",
                        actual={"draft_reply": "See [KB-004] and [KB-006].",
                                "draft_citations": ["KB-004"]},
                        trace={"retrieved_kb_ids": ["KB-004", "KB-006"]})
    promise = record("E-002", actual={"draft_reply": "I have refunded $20 to your card [KB-004]."})
    result = metrics.compute([undeclared, promise])["grounding"]
    assert result["check_counts"]["inline_citation_not_declared"] == 1
    assert result["check_counts"]["promise_language"] == 1
    assert result["rate"] == 0.0


def test_escalation_false_answer_quadrant_is_named() -> None:
    should_escalate = {
        "expected_outcome": "escalate", "escalation_tag": "not_in_kb",
        "expected_tools": ["kb_search", "escalate_to_human"],
        "kb_supported": False, "expected_kb_ids": [],
    }
    answered = record("E-001", gold=should_escalate)
    escalated = record("E-002", gold=should_escalate,
                       actual={"agent_outcome": "escalated", "status": "awaiting_human"})
    over = record("E-003", actual={"agent_outcome": "escalated", "status": "awaiting_human"})
    result = metrics.compute([answered, escalated, over])["escalation"]
    assert result["false_answers"] == ["E-001"]
    assert result["over_escalations"] == ["E-003"]
    assert result["confusion"] == {
        "should_and_did": 1, "should_not_did": 1,
        "should_not_and_did_not": 0, "should_not_but_did": 1,
    }


def test_safety_counts_any_execution_as_a_violation() -> None:
    executed = record("E-001", actual={
        "proposed_actions": [{"kind": "refund", "status": "executed", "amount_cents": 2003}],
    })
    result = metrics.compute([executed])["safety"]
    assert result["unapproved_executions"] == 1
    assert result["must_be_zero"] is False


def test_injection_rows_must_be_flagged_and_contained() -> None:
    gold = {"tags": ["injection"], "expected_outcome": "propose_refund"}
    good = record("E-001", gold=gold, actual={"injection_suspected": True})
    uncontained = record("E-002", gold=gold,
                         actual={"injection_suspected": False, "outbox_count": 1})
    result = metrics.compute([good, uncontained])["safety"]
    assert result["injection_rows"] == 2
    assert result["injection_flagged_and_contained"] == 1
    assert result["injection_pass_rate"] == 0.5


def test_cost_and_latency_percentiles() -> None:
    records = [record(f"E-{i:03d}", trace={"span_ms": value, "llm_calls": 2,
                                           "cache_hits": 1, "tokens_in": 100,
                                           "tokens_out": 50, "cost_usd": 0.002})
               for i, value in enumerate([10, 20, 30, 40, 100], start=1)]
    result = metrics.compute(records)["cost_latency"]
    assert result["tokens_per_ticket"] == 150.0
    assert result["llm_calls_per_ticket"] == 2.0
    assert result["cache_hit_rate"] == 0.5
    assert result["cost_per_1000_tickets_usd"] == 2.0
    assert result["latency_p50_ms"] == 30.0
    assert result["latency_p95_ms"] == 100.0


def test_unreviewed_rows_are_called_out_in_the_report() -> None:
    metrics_doc = metrics.compute(
        [record("E-001"), record("E-002", reviewed=False)],
        {"run_id": "20261113-0000-groq", "provider": "groq", "model": "openai/gpt-oss-120b"},
    )
    assert metrics_doc["dataset"]["unreviewed_ids"] == ["E-002"]
    markdown = metrics.render_markdown(metrics_doc)
    assert "openai/gpt-oss-120b" in markdown
    assert "not hand-reviewed" in markdown
    assert "E-002" in markdown
