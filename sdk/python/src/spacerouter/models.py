"""Response models for the SpaceRouter SDK."""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, computed_field, model_validator

# ---------------------------------------------------------------------------
# Routing & filtering types
# ---------------------------------------------------------------------------

IpType = Literal["residential", "mobile", "business"]
"""IP address type for filtering proxy nodes."""

NodeStatus = Literal["offline", "draining"]
"""Node operational status (for status updates). Nodes go online via health probes."""

NodeConnectivityType = Literal["direct", "upnp", "external_provider"]
"""How a node connects to the network."""

# ---------------------------------------------------------------------------
# API key models
# ---------------------------------------------------------------------------


class ApiKey(BaseModel):
    """API key returned at creation time (POST /api-keys).

    The raw ``api_key`` value is only available in this response.
    """

    id: str
    name: str
    api_key: str
    rate_limit_rpm: int


class ApiKeyInfo(BaseModel):
    """API key metadata returned by list endpoint (GET /api-keys).

    The raw key is never included — only ``key_prefix`` (first 12 chars).
    """

    id: str
    name: str
    key_prefix: str
    rate_limit_rpm: int
    is_active: bool
    created_at: str


# ---------------------------------------------------------------------------
# Node management models
# ---------------------------------------------------------------------------


class Node(BaseModel):
    """Proxy node returned by ``GET /nodes`` and ``POST /nodes``.

    v0.2.0 uses three role-specific wallet addresses.  The legacy
    ``wallet_address`` field is kept as a computed alias that returns
    ``identity_address`` for backward compatibility.
    """

    id: str
    endpoint_url: str
    public_ip: str
    connectivity_type: str
    node_type: str
    status: str
    health_score: float
    region: str
    label: str | None = None
    ip_type: str
    ip_region: str
    as_type: str
    identity_address: str
    staking_address: str
    collection_address: str
    created_at: str
    gateway_ca_cert: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def wallet_address(self) -> str:
        """Backward-compatible alias — returns ``identity_address``."""
        return self.identity_address

    @model_validator(mode="before")
    @classmethod
    def _migrate_wallet_address(cls, data: Any) -> Any:
        """Accept legacy payloads that only contain ``wallet_address``."""
        if isinstance(data, dict) and "wallet_address" in data:
            for field in ("identity_address", "staking_address", "collection_address"):
                data.setdefault(field, data["wallet_address"])
        return data


# ---------------------------------------------------------------------------
# Staking registration models
# ---------------------------------------------------------------------------


class RegisterChallenge(BaseModel):
    """Challenge returned by ``POST /nodes/register/challenge``."""

    nonce: str
    expires_in: int


class RegisterResult(BaseModel):
    """Result of ``POST /nodes/register/verify``."""

    status: str
    node_id: str
    identity_address: str
    staking_address: str
    collection_address: str
    endpoint_url: str
    gateway_ca_cert: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def address(self) -> str:
        """Backward-compatible alias — returns ``identity_address``."""
        return self.identity_address

    @model_validator(mode="before")
    @classmethod
    def _migrate_address(cls, data: Any) -> Any:
        """Accept legacy payloads that only contain ``address``."""
        if isinstance(data, dict) and "address" in data:
            data.setdefault("identity_address", data["address"])
            data.setdefault("staking_address", data.get("identity_address", data["address"]))
            data.setdefault("collection_address", data.get("identity_address", data["address"]))
        return data


# ---------------------------------------------------------------------------
# Billing models
# ---------------------------------------------------------------------------


class CheckoutSession(BaseModel):
    """Checkout session returned by ``POST /billing/checkout``."""

    checkout_url: str


class BillingReissueResult(BaseModel):
    """Reissued API key returned by ``POST /billing/reissue``."""

    new_api_key: str


# ---------------------------------------------------------------------------
# Dashboard models
# ---------------------------------------------------------------------------


class Transfer(BaseModel):
    """Single data transfer record."""

    request_id: str
    bytes: int
    method: str
    target_host: str
    created_at: str


class TransferPage(BaseModel):
    """Paginated transfer list from ``GET /dashboard/transfers``."""

    page: int
    total_pages: int
    total_bytes: int
    transfers: list[Transfer]


# ---------------------------------------------------------------------------
# Credit line models (v0.2.0)
# ---------------------------------------------------------------------------

CreditLineStatusType = Literal["active", "suspended", "pending"]


class CreditLineStatus(BaseModel):
    """Credit line status from ``GET /credit-lines/{address}``."""

    address: str
    credit_limit: float
    used: float
    available: float
    status: CreditLineStatusType
    foundation_managed: bool


class VouchingSignature(BaseModel):
    """Vouching signature proving identity wallet vouches for staking wallet."""

    identity_address: str
    staking_address: str
    signature: str
    timestamp: int


CONNECT_METADATA_EXTENSION = "spacerouter_connect_metadata"
"""``httpx.Response.extensions`` key holding the captured CONNECT headers."""

HEADER_NODE_ID = "x-spacerouter-node"
HEADER_REQUEST_ID = "x-spacerouter-request-id"
HEADER_ROUTING_TAG = "x-spacerouter-routing"


def read_response_metadata(response: httpx.Response, header: str) -> str | None:
    """Read a SpaceRouter header, preferring the captured CONNECT response."""
    captured = response.extensions.get(CONNECT_METADATA_EXTENSION) or {}
    return captured.get(header) or response.headers.get(header)


class ProxyResponse:
    """Thin wrapper around :class:`httpx.Response` with SpaceRouter metadata.

    Exposes ``request_id``, ``node_id`` and ``routing_tag`` and delegates
    everything else to the underlying httpx response.

    For HTTP target URLs the gateway injects the ``X-SpaceRouter-*``
    headers into the inner response. For HTTPS target URLs the inner
    exchange is end-to-end TLS and opaque to the gateway, so it stamps
    the same headers on the ``CONNECT 200`` response instead. httpcore
    consumes that CONNECT response internally, so the client captures
    the headers at the connection layer and hands them over in
    ``response.extensions[CONNECT_METADATA_EXTENSION]``. Both target
    schemes therefore populate all three properties; SOCKS5 has no
    CONNECT header concept and only the inner-response fallback applies.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def request_id(self) -> str | None:
        """Unique request ID for tracing (``X-SpaceRouter-Request-Id``)."""
        return read_response_metadata(self._response, HEADER_REQUEST_ID)

    @property
    def node_id(self) -> str | None:
        """ID of the Provider that served this request (``X-SpaceRouter-Node``)."""
        return read_response_metadata(self._response, HEADER_NODE_ID)

    @property
    def routing_tag(self) -> str | None:
        """Gateway routing decision — ``"home"`` or ``"fallback"``."""
        return read_response_metadata(self._response, HEADER_ROUTING_TAG)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def __repr__(self) -> str:
        return f"<ProxyResponse [{self._response.status_code}]>"
