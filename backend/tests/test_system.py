"""Tests for system endpoints (health + application identification)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    """A TestClient over a fresh application instance.

    The MCP session manager is single-use per instance, so each test that
    enters the app lifespan needs its own app built by ``create_app`` rather
    than reusing the module-level ``app``.
    """
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "ledgerflow-backend"


def test_root_endpoint(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "LedgerFlow"
    assert body["service"] == "ledgerflow-backend"
    assert body["phase"] == "1"


def test_cors_allows_frontend_origin(client: TestClient) -> None:
    response = client.get(
        "/api/health",
        headers={"Origin": "http://localhost:3000"},
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
