"""Tests for the routing service (node selection and health scoring)."""

import httpx
import pytest

from app.config import Settings
from app.services.routing_service import RoutingService


class TestProxyjetEndpointUrl:
    def test_builds_correct_url(self):
        settings = Settings(
            USE_SQLITE=False,
            PROXYJET_HOST="proxy.proxyjet.io",
            PROXYJET_PORT=8080,
            PROXYJET_USERNAME="user",
            PROXYJET_PASSWORD="pass",
        )
        http_client = httpx.AsyncClient()
        service = RoutingService(http_client, settings)
        url = service._proxyjet_endpoint_url("user:pass")
        assert url == "http://user:pass@proxy.proxyjet.io:8080"


class TestSelectNode:
    @pytest.mark.asyncio
    async def test_selects_sqlite_node(self, tmp_path):
        settings = Settings(
            USE_SQLITE=True,
            SQLITE_DB_PATH=str(tmp_path / "test.db"),
        )
        http_client = httpx.AsyncClient()
        service = RoutingService(http_client, settings)
        result = await service.select_node()

        assert result is not None
        assert result.node_id != ""
        assert result.endpoint_url != ""

    @pytest.mark.asyncio
    async def test_fallback_to_proxyjet_when_not_sqlite(self):
        settings = Settings(
            USE_SQLITE=False,
            PROXYJET_HOST="proxy.proxyjet.io",
            PROXYJET_PORT=8080,
            PROXYJET_USERNAME="user",
            PROXYJET_PASSWORD="pass",
        )
        http_client = httpx.AsyncClient()
        service = RoutingService(http_client, settings)
        result = await service.select_node()

        assert result is not None
        assert result.node_id == "proxyjet-fallback"
        assert "proxy.proxyjet.io" in result.endpoint_url

    @pytest.mark.asyncio
    async def test_returns_none_when_no_nodes_and_no_proxyjet(self):
        settings = Settings(
            USE_SQLITE=False,
            PROXYJET_HOST="",
        )
        http_client = httpx.AsyncClient()
        service = RoutingService(http_client, settings)
        result = await service.select_node()

        assert result is None


class TestReportOutcome:
    @pytest.mark.asyncio
    async def test_records_outcome_sqlite(self, tmp_path):
        settings = Settings(
            USE_SQLITE=True,
            SQLITE_DB_PATH=str(tmp_path / "test.db"),
        )
        http_client = httpx.AsyncClient()
        service = RoutingService(http_client, settings)

        # First select a node (populates cache)
        node = await service.select_node()
        assert node is not None

        # Report a successful outcome
        await service.report_outcome(node.node_id, True, 100, 1024)
        # Should not raise
