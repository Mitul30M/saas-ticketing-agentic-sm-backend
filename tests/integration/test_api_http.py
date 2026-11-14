"""HTTP-boundary matrix: auth, validation, size limit, idempotent POSTs."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import worker
from app.config import settings
from app.main import app
from app.tools.registry import build_registry
from app.tools.schemas import ToolContext

pytestmark = pytest.mark.integration

TOKEN = "itst-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

PROTECTED_GET = [
    "/api/tickets",
    "/api/tickets/00000000-0000-0000-0000-000000000000",
    "/api/tickets/00000000-0000-0000-0000-000000000000/trace",
    "/api/approvals/pending",
    "/api/customers/00000000-0000-0000-0000-000000000000",
    "/api/kb/articles",
    "/api/kb/articles/KB-001",
    "/api/kb/search?q=refund",
    "/api/metrics/summary",
]
PROTECTED_POST = [
    (f"/api/tickets/{uuid.uuid4()}/run", None),
    (f"/api/approvals/send_reply/{uuid.uuid4()}/decide", {"decision": "approve"}),
]


@pytest.fixture()
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "agent_token", TOKEN)
    return TOKEN


def test_every_dashboard_route_fails_closed_without_a_token(
    migrated_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "agent_token", "")
    with TestClient(app) as client:
        for path in PROTECTED_GET:
            assert client.get(path).status_code == 503, path
        for path, body in PROTECTED_POST:
            assert client.post(path, json=body).status_code == 503, path
        # intake is customer-facing (no dashboard token), so it validates instead
        intake = client.post(
            "/api/tickets",
            json={"subject": "ITST intake", "body": "b", "from_email": "a@b.test"},
        )
        assert intake.status_code == 400
        assert client.get("/healthz").status_code == 200


def test_every_dashboard_route_requires_the_configured_token(
    migrated_conn: Any, token: str
) -> None:
    with TestClient(app) as client:
        for path in PROTECTED_GET:
            assert client.get(path).status_code == 401, path
            assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
            assert client.get(path, headers=AUTH).status_code != 401, path
        for path, body in PROTECTED_POST:
            assert client.post(path, json=body).status_code == 401, path


def test_mutating_routes_require_headers_before_validation(
    migrated_conn: Any, token: str
) -> None:
    with TestClient(app) as client:
        payload = {"subject": "ITST HTTP validation", "body": "A ticket body",
                   "from_email": "http@example.test"}
        # intake is public: the header check comes first, not the dashboard token
        assert client.post("/api/tickets", json=payload).status_code == 400

        missing_key = client.post("/api/tickets", json=payload, headers=AUTH)
        assert missing_key.status_code == 400
        assert "Idempotency-Key" in missing_key.json()["detail"]

        run = client.post(
            f"/api/tickets/{uuid.uuid4()}/run",
            headers={**AUTH, "Idempotency-Key": f"itst-http-{uuid.uuid4().hex}"},
        )
        assert run.status_code == 404  # unknown ticket, key accepted

        decide = client.post(
            f"/api/approvals/send_reply/{uuid.uuid4()}/decide",
            headers={
                **AUTH,
                "Idempotency-Key": f"itst-http-{uuid.uuid4().hex}",
            },
            json={"decision": "approve"},
        )
        assert decide.status_code == 400
        assert "X-Operator" in decide.json()["detail"]

        unknown_kind = client.post(
            f"/api/approvals/nonsense/{uuid.uuid4()}/decide",
            headers={
                **AUTH,
                "X-Operator": "mia",
                "Idempotency-Key": f"itst-http-{uuid.uuid4().hex}",
            },
            json={"decision": "approve"},
        )
        assert unknown_kind.status_code == 404


def test_body_limit_is_enforced_before_buffering(migrated_conn: Any, token: str) -> None:
    with TestClient(app) as client:
        headers = {**AUTH, "Idempotency-Key": f"itst-size-{uuid.uuid4().hex}"}
        oversized = {
            "subject": "ITST too big",
            "body": "x" * (settings.max_request_bytes + 1000),
            "from_email": "big@example.test",
        }
        assert client.post("/api/tickets", json=oversized, headers=headers).status_code == 413

        # no Content-Length (chunked) still hits the size check
        chunked = (b"x" * 8192 for _ in range(settings.max_request_bytes // 8192 + 2))
        response = client.post(
            "/api/tickets",
            content=chunked,
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 413


def test_ticket_detail_returns_flags_and_proposed_actions(
    db_pool: Any, migrated_conn: Any, two_customers: Any, token: str
) -> None:
    ticket = two_customers.ticket_id
    migrated_conn.execute(
        "UPDATE tickets SET flags=ARRAY['injection_suspected'], queue='billing' WHERE id=%s",
        (ticket,),
    )
    result = build_registry(db_pool).call(
        "propose_refund",
        {
            "ticket_id": ticket,
            "customer_id": str(two_customers.alice_id),
            "invoice_id": two_customers.inv_a,
            "amount_cents": 500,
            "reason": "HTTP detail test",
            "kb_ids": ["KB-006"],
            "idempotency_key": f"itst-http-detail-{ticket}",
        },
        ticket_id=ticket,
        ctx=ToolContext(trace_id=f"http-detail-{ticket}", timeout_s=5),
    )
    assert result.ok is True

    with TestClient(app) as client:
        response = client.get(f"/api/tickets/{ticket}", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["flags"] == ["injection_suspected"]
    assert body["proposed_actions"][0]["kind"] == "refund"
    assert body["proposed_actions"][0]["ref"] == str(result.data["refund_request_id"])
    # nothing has been sent yet, so the dashboard has no sent reply to show
    assert body["sent_reply"] is None
    # dashboard header fields: who wrote it and when
    assert body["from_email"] == two_customers.alice_email
    assert body["received_at"]

    with TestClient(app) as client:
        assert client.get(
            f"/api/tickets/{uuid.uuid4()}", headers=AUTH
        ).status_code == 404


def test_kb_article_by_id_opens_a_citation(migrated_conn: Any, token: str) -> None:
    """A [KB-xxx] citation in a draft must be openable by id (Phase 5 requirement)."""
    with TestClient(app) as client:
        article = client.get("/api/kb/articles/KB-006", headers=AUTH)
        unknown = client.get("/api/kb/articles/KB-9999", headers=AUTH)
    assert article.status_code == 200
    payload = article.json()
    assert payload["id"] == "KB-006"
    assert payload["title"] and payload["body"]
    assert unknown.status_code == 404


def test_decide_edit_rules_on_a_real_reply(
    db_pool: Any, migrated_conn: Any, two_customers: Any, token: str
) -> None:
    ticket = str(
        migrated_conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, "
            "status, draft_reply) VALUES (%s,%s,%s,%s,%s,'awaiting_approval',%s) RETURNING id",
            (
                two_customers.alice_id,
                f"ITST http edit {uuid.uuid4().hex[:8]}",
                "How does the refund policy work?",
                f"itst-http-edit-{uuid.uuid4().hex}",
                two_customers.alice_email,
                "Refunds are available within 30 days [KB-006].",
            ),
        ).fetchone()["id"]
    )
    headers = {
        **AUTH,
        "X-Operator": "mia",
        "Idempotency-Key": f"itst-http-edit-{ticket}",
    }
    with TestClient(app) as client:
        no_text = client.post(
            f"/api/approvals/send_reply/{ticket}/decide",
            headers=headers, json={"decision": "edit"},
        )
        assert no_text.status_code == 422
        assert "edited_text" in no_text.json()["detail"]

        approve_with_text = client.post(
            f"/api/approvals/send_reply/{ticket}/decide",
            headers=headers, json={"decision": "approve", "edited_text": "changed"},
        )
        assert approve_with_text.status_code == 422

        refund_id = str(uuid.uuid4())
        edit_refund = client.post(
            f"/api/approvals/refund/{refund_id}/decide",
            headers={**headers, "Idempotency-Key": f"itst-http-refund-{refund_id}"},
            json={"decision": "edit", "edited_text": "nope"},
        )
        assert edit_refund.status_code == 422

        accepted = client.post(
            f"/api/approvals/send_reply/{ticket}/decide",
            headers=headers, json={"decision": "edit", "edited_text": "Edited reply [KB-006]."},
        )
        assert accepted.status_code == 202
        assert migrated_conn.execute(
            "SELECT edited_text FROM approvals WHERE action_type='send_reply' AND action_ref=%s",
            (ticket,),
        ).fetchone()["edited_text"] == "Edited reply [KB-006]."


def test_repeated_ticket_post_creates_one_ticket(
    db_pool: Any, migrated_conn: Any, token: str
) -> None:
    key = f"itst-http-replay-{uuid.uuid4().hex}"
    payload = {
        "subject": "ITST HTTP replay",
        "body": "One submission replayed five times.",
        "from_email": f"replay.{uuid.uuid4().hex[:8]}@example.test",
    }
    with TestClient(app) as client:
        responses = [
            client.post(
                "/api/tickets", json=payload,
                headers={**AUTH, "Idempotency-Key": key},
            )
            for _ in range(5)
        ]
    assert {response.status_code for response in responses} == {201}
    ids = {response.json()["ticket_id"] for response in responses}
    assert len(ids) == 1
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM tickets WHERE from_email=%s AND subject=%s",
        (payload["from_email"], payload["subject"]),
    ).fetchone()["n"] == 1

    # a different key with the same content still collapses via the soft dedupe
    with TestClient(app) as client:
        other = client.post(
            "/api/tickets", json=payload,
            headers={**AUTH, "Idempotency-Key": f"{key}-other"},
        )
    assert other.status_code == 201
    assert other.json()["ticket_id"] == next(iter(ids))
    assert other.json()["duplicate"] is True


def test_rerun_endpoint_reopens_a_resolved_ticket(
    db_pool: Any, migrated_conn: Any, two_customers: Any, token: str
) -> None:
    ticket = str(
        migrated_conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, content_hash, from_email, status) "
            "VALUES (%s,%s,%s,%s,%s,'resolved') RETURNING id",
            (
                two_customers.alice_id,
                f"ITST http rerun {uuid.uuid4().hex[:8]}",
                "How does the refund policy work?",
                f"itst-http-rerun-{uuid.uuid4().hex}",
                two_customers.alice_email,
            ),
        ).fetchone()["id"]
    )
    with TestClient(app) as client:
        accepted = client.post(
            f"/api/tickets/{ticket}/run",
            headers={**AUTH, "Idempotency-Key": f"itst-http-rerun-{ticket}"},
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        assert migrated_conn.execute(
            "SELECT status FROM tickets WHERE id=%s", (ticket,)
        ).fetchone()["status"] == "triaging"

        # a second run while one is queued is refused
        duplicate = client.post(
            f"/api/tickets/{ticket}/run",
            headers={**AUTH, "Idempotency-Key": f"itst-http-rerun-2-{ticket}"},
        )
        assert duplicate.status_code == 409
    migrated_conn.execute("UPDATE jobs SET status='done' WHERE id=%s", (job_id,))


def test_http_submit_run_decide_approve_flow(
    db_pool: Any, migrated_conn: Any, two_customers: Any, token: str
) -> None:
    """PLAN Phase 4 DoD over HTTP: submit -> pause -> approve -> one outbox row."""
    with TestClient(app) as client:
        created = client.post(
            "/api/tickets",
            json={
                "subject": "ITST HTTP flow",
                "body": "Please explain the refund policy and the refund window.",
                "from_email": two_customers.alice_email,
            },
            headers={**AUTH, "Idempotency-Key": f"itst-http-flow-{uuid.uuid4().hex}"},
        )
    assert created.status_code == 201
    ticket = created.json()["ticket_id"]

    for _ in range(10):
        worker.run_one(db_pool, "itst-http-worker")
        status = migrated_conn.execute(
            "SELECT status FROM tickets WHERE id=%s", (ticket,)
        ).fetchone()["status"]
        if status not in {"new", "triaging"}:
            break
    assert status == "awaiting_approval"

    with TestClient(app) as client:
        detail = client.get(f"/api/tickets/{ticket}", headers=AUTH)
        assert detail.status_code == 200
        assert detail.json()["draft_reply"]

        pending = client.get("/api/approvals/pending", headers=AUTH)
        kinds_for_ticket = {
            item["kind"] for item in pending.json()["items"]
            if item["ticket_id"] == ticket
        }
        assert kinds_for_ticket == {"send_reply"}

        decision = client.post(
            f"/api/approvals/send_reply/{ticket}/decide",
            headers={
                **AUTH,
                "X-Operator": "mia",
                "Idempotency-Key": f"itst-http-flow-decide-{ticket}",
            },
            json={"decision": "approve"},
        )
    assert decision.status_code == 202, decision.text

    for _ in range(10):
        worker.run_one(db_pool, "itst-http-worker")
        sent = migrated_conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE ticket_id=%s", (ticket,)
        ).fetchone()["n"]
        if sent:
            break
    assert sent == 1
    assert migrated_conn.execute(
        "SELECT status FROM tickets WHERE id=%s", (ticket,)
    ).fetchone()["status"] == "resolved"
    assert migrated_conn.execute(
        "SELECT count(*) AS n FROM audit_log WHERE event='reply_executed' AND entity_id=%s",
        (ticket,),
    ).fetchone()["n"] == 1

    # the detail now exposes the message that was actually sent, and who approved it
    with TestClient(app) as client:
        detail_after = client.get(f"/api/tickets/{ticket}", headers=AUTH).json()
    assert detail_after["sent_reply"]["approved_by"] == "mia"
    assert detail_after["sent_reply"]["body"]

    with TestClient(app) as client:
        trace = client.get(f"/api/tickets/{ticket}/trace", headers=AUTH)
        metrics = client.get("/api/metrics/summary", headers=AUTH)
    assert trace.status_code == 200 and trace.json()["items"]
    assert metrics.status_code == 200
    assert metrics.json()["trace"]["shadow_price_model"] == "gpt-4o-mini"


def test_metrics_summary_reports_counts(migrated_conn: Any, token: str) -> None:
    migrated_conn.execute(
        "INSERT INTO trace_events (trace_id, ticket_id, node, kind, duration_ms, "
        "tokens_in, tokens_out, cost_usd, cache_hit) "
        "VALUES ('itst-http-metrics','itst-http-metrics','triage','llm',12,10,5,0,false)"
    )
    try:
        with TestClient(app) as client:
            response = client.get("/api/metrics/summary", headers=AUTH)
    finally:
        migrated_conn.execute(
            "DELETE FROM trace_events WHERE trace_id='itst-http-metrics'"
        )
    assert response.status_code == 200
    payload = response.json()
    for key in (
        "tickets_by_status",
        "outcomes",
        "tickets_by_category",
        "tickets_by_queue",
        "escalations_by_reason",
        "node_latency",
        "jobs",
    ):
        assert key in payload
    assert "actual_cost_usd" in payload["trace"]
    assert "shadow_cost_usd" in payload["trace"]
    assert "llm_cache_hit_rate" in payload["trace"]
    assert "tool_error_rate" in payload["trace"]


def test_admin_routes_are_absent_by_default(migrated_conn: Any, token: str) -> None:
    with TestClient(app) as client:
        assert client.post("/api/admin/seed", headers=AUTH).status_code == 404


def test_trace_exposes_the_tool_error_message_without_payloads(
    migrated_conn: Any, two_customers: Any, token: str
) -> None:
    """A failed tool used to surface only its code ("business_rule"); the sentence
    that names the rule is what an operator needs. Whole payloads stay private."""
    ticket = two_customers.ticket_id
    migrated_conn.execute(
        "INSERT INTO trace_events (trace_id, ticket_id, node, kind, status, error, output) "
        "VALUES ('itst-trace-detail', %s, 'propose_refund', 'tool', 'error', "
        "'business_rule', %s)",
        (
            str(ticket),
            ('{"error": {"code": "business_rule", "message": '
             '"invoice is outside the 30-day refund window", "retryable": false}}'),
        ),
    )
    try:
        with TestClient(app) as client:
            items = client.get(f"/api/tickets/{ticket}/trace", headers=AUTH).json()["items"]
    finally:
        migrated_conn.execute("DELETE FROM trace_events WHERE trace_id='itst-trace-detail'")

    row = next(item for item in items if item["trace_id"] == "itst-trace-detail")
    assert row["error_detail"] == "invoice is outside the 30-day refund window"
    assert "output" not in row
