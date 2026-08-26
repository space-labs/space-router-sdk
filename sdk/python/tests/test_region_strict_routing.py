"""An explicitly requested region must not be silently downgraded.

QA v1.5.2-test.136 (Jenna Lee): `request get --pay --region AQ` returned
502 "Target unreachable via residential node", and a Leg 1 receipt was minted
and later auto-settled for a request that produced no usable response. There is
no provider in AQ, so the gateway fell back to the managed pool and billed for
the failure.

The gateway has always supported X-SpaceRouter-Strict-Routing, which makes a
filter miss surface as 503 before any request is made. Neither SDK ever sent
it, so region was only ever a soft hint.
"""
from __future__ import annotations

import pytest

from spacerouter.client import _build_proxy


def _headers(**kwargs) -> dict:
    proxy = _build_proxy("sr_live_test", "http://gw.test:8080", "http", **kwargs)
    return {k.lower(): v for k, v in proxy.headers.items()}


def test_region_sends_strict_routing():
    headers = _headers(region="US", ip_type=None)
    assert headers.get("x-spacerouter-region") == "US"
    assert headers.get("x-spacerouter-strict-routing") == "1", (
        "a requested region must be honoured or error, never silently "
        "downgraded to the fallback pool"
    )


def test_no_region_does_not_send_strict_routing():
    headers = _headers(region=None, ip_type=None)
    assert "x-spacerouter-strict-routing" not in headers
    assert "x-spacerouter-region" not in headers


def test_ip_type_alone_does_not_force_strict_routing():
    """ip_type already 503s on a miss; only region needed the guarantee."""
    headers = _headers(region=None, ip_type="mobile")
    assert headers.get("x-spacerouter-ip-type") == "mobile"
    assert "x-spacerouter-strict-routing" not in headers


def test_region_and_ip_type_together_send_strict_routing():
    headers = _headers(region="JP", ip_type="mobile")
    assert headers.get("x-spacerouter-region") == "JP"
    assert headers.get("x-spacerouter-ip-type") == "mobile"
    assert headers.get("x-spacerouter-strict-routing") == "1"


def test_datacenter_is_no_longer_an_advertised_ip_type():
    """The gateway's valid set is residential/mobile/business/hosting.

    `datacenter` was in the SDK's IpType and the CLI --help but was rejected
    with 400 Bad Request. It is a fallback tier, not an operator choice.
    """
    import typing

    from spacerouter.models import IpType

    assert set(typing.get_args(IpType)) == {"residential", "mobile", "business"}
