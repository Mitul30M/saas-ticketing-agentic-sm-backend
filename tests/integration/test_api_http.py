"""HTTP-boundary coverage for authentication, validation, and headers."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.dependencies import protected
from app.config import settings
from app.main import app


def test_protected_routes_fail_closed_and_accept_only_configured_token(
    migrated_conn: object,
) -> None:
    original = settings.agent_token
    try:
        settings.agent_token = ""
        with TestClient(app) as client:
            assert client.get("/api/tickets").status_code == 503
        settings.agent_token = "test-token"
        with TestClient(app) as client:
            assert client.get("/api/tickets").status_code == 401
            assert client.get(
                "/api/tickets", headers={"Authorization": "Bearer wrong"}
            ).status_code == 401
            assert client.get(
                "/api/tickets", headers={"Authorization": "Bearer test-token"}
            ).status_code == 200
            assert client.get(
                "/api/tickets/not-a-uuid",
                headers={"Authorization": "Bearer test-token"},
            ).status_code == 422
            try:
                protected("Bearer \N{SNOWMAN}")
            except HTTPException as exc:
                assert exc.status_code == 401
            else:
                raise AssertionError("non-ASCII token must be rejected")
    finally:
        settings.agent_token = original


def test_http_mutation_headers_and_decision_validation(migrated_conn: object) -> None:
    original = settings.agent_token
    settings.agent_token = "test-token"
    headers = {"Authorization": "Bearer test-token"}
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/tickets",
                json={
                    "subject": "HTTP validation",
                    "body": "A ticket body",
                    "from_email": "http@example.test",
                },
            )
            assert response.status_code == 400
            response = client.post(
                "/api/approvals/send_reply/not-a-uuid/decide",
                headers=headers,
                json={"decision": "edit"},
            )
            assert response.status_code == 422
    finally:
        settings.agent_token = original
