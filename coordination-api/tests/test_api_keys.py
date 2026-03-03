"""Tests for the API key management endpoints."""

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.services.auth_service import AuthService
from app.services.routing_service import RoutingService
from app.sqlite_db import SQLiteClient


def _setup_app(settings: Settings) -> TestClient:
    http_client = httpx.AsyncClient()
    db = SQLiteClient(settings.SQLITE_DB_PATH)
    app.state.settings = settings
    app.state.http_client = http_client
    app.state.db = db
    app.state.auth_service = AuthService(http_client, settings)
    app.state.routing_service = RoutingService(http_client, settings)
    return TestClient(app, raise_server_exceptions=False)


class TestCreateApiKey:
    def test_create_success(self, settings):
        client = _setup_app(settings)
        resp = client.post(
            "/api-keys",
            json={"name": "My Agent Key"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Agent Key"
        assert data["api_key"].startswith("sr_live_")
        assert len(data["api_key"]) == 56  # "sr_live_" (8) + 48 hex chars
        assert data["rate_limit_rpm"] == 60

    def test_create_with_custom_rpm(self, settings):
        client = _setup_app(settings)
        resp = client.post(
            "/api-keys",
            json={"name": "High RPM Key", "rate_limit_rpm": 200},
        )
        assert resp.status_code == 201
        assert resp.json()["rate_limit_rpm"] == 200


class TestListApiKeys:
    def test_list_keys(self, settings):
        client = _setup_app(settings)
        # Create two keys first
        client.post("/api-keys", json={"name": "Key 1"})
        client.post("/api-keys", json={"name": "Key 2"})
        resp = client.get("/api-keys")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Raw api_key never returned in list
        assert "api_key" not in data[0]


class TestRevokeApiKey:
    def test_revoke(self, settings):
        client = _setup_app(settings)
        # Create a key first
        create_resp = client.post("/api-keys", json={"name": "Temp Key"})
        key_id = create_resp.json()["id"]
        # Revoke it
        resp = client.delete(f"/api-keys/{key_id}")
        assert resp.status_code == 204
