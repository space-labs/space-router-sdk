"""Tests for the internal endpoints (proxy-gateway contract)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.services.auth_service import AuthService
from app.services.routing_service import RoutingService
from app.sqlite_db import SQLiteClient


def _setup_app(settings: Settings) -> TestClient:
    """Wire up the app with test dependencies."""
    http_client = httpx.AsyncClient()
    db = SQLiteClient(settings.SQLITE_DB_PATH)
    app.state.settings = settings
    app.state.http_client = http_client
    app.state.db = db
    app.state.auth_service = AuthService(http_client, settings)
    app.state.routing_service = RoutingService(http_client, settings)

    # Override cached settings so verify_internal_secret uses our test settings
    get_settings.cache_clear()
    import app.config as config_module
    config_module.get_settings = lambda: settings

    return TestClient(app, raise_server_exceptions=False)


class TestAuthValidate:
    def test_valid_key(self, settings):
        """SQLite auth stub approves all keys for local testing."""
        client = _setup_app(settings)
        resp = client.post(
            "/internal/auth/validate",
            json={"key_hash": "abc123hash"},
            headers={"X-Internal-API-Key": settings.INTERNAL_API_SECRET},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["api_key_id"] is not None
        assert data["rate_limit_rpm"] is not None

    def test_missing_auth_header(self, settings):
        client = _setup_app(settings)
        resp = client.post(
            "/internal/auth/validate",
            json={"key_hash": "abc123hash"},
        )
        assert resp.status_code == 403  # APIKeyHeader missing

    def test_wrong_auth_secret_rejected_in_production_mode(self):
        """Non-SQLite mode rejects wrong auth secrets."""
        prod_settings = Settings(
            PORT=8000,
            INTERNAL_API_SECRET="test-secret",
            USE_SQLITE=False,
        )
        get_settings.cache_clear()
        # Patch in both modules so the dependency picks it up
        import app.config as config_module
        import app.dependencies as deps_module
        original = deps_module.get_settings
        config_module.get_settings = lambda: prod_settings
        deps_module.get_settings = lambda: prod_settings

        http_client = httpx.AsyncClient()
        app.state.settings = prod_settings
        app.state.http_client = http_client
        app.state.auth_service = AuthService(http_client, prod_settings)
        app.state.routing_service = RoutingService(http_client, prod_settings)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/internal/auth/validate",
            json={"key_hash": "abc123hash"},
            headers={"X-Internal-API-Key": "wrong-secret"},
        )
        assert resp.status_code == 403

        # Restore
        deps_module.get_settings = original


class TestRouteSelect:
    def test_selects_local_node(self, settings):
        """SQLite mode creates a local test node."""
        client = _setup_app(settings)
        resp = client.get(
            "/internal/route/select",
            headers={"X-Internal-API-Key": settings.INTERNAL_API_SECRET},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_id"] != ""
        assert data["endpoint_url"] != ""

    def test_no_nodes_no_proxyjet(self):
        """No proxyjet configured and non-SQLite mode -> 503."""
        no_proxyjet_settings = Settings(
            PORT=8000,
            INTERNAL_API_SECRET="test-secret",
            USE_SQLITE=False,
            PROXYJET_HOST="",  # Not configured
        )

        get_settings.cache_clear()
        import app.config as config_module
        config_module.get_settings = lambda: no_proxyjet_settings

        http_client = httpx.AsyncClient()
        app.state.settings = no_proxyjet_settings
        app.state.http_client = http_client
        app.state.auth_service = AuthService(http_client, no_proxyjet_settings)
        app.state.routing_service = RoutingService(http_client, no_proxyjet_settings)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/internal/route/select",
            headers={"X-Internal-API-Key": no_proxyjet_settings.INTERNAL_API_SECRET},
        )
        assert resp.status_code == 503


class TestRouteReport:
    def test_report_success(self, settings):
        client = _setup_app(settings)
        resp = client.post(
            "/internal/route/report",
            json={
                "node_id": "node-abc",
                "success": True,
                "latency_ms": 150,
                "bytes": 4096,
            },
            headers={"X-Internal-API-Key": settings.INTERNAL_API_SECRET},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
