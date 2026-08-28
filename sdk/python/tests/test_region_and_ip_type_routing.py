"""A requested region is honoured without disabling the managed fallback.

QA v1.5.2-test.136 (Jenna Lee): `request get --pay --region AQ` returned 502
"Target unreachable via residential node", and a Leg 1 receipt was minted and
later auto-settled for a request that produced no usable response.

Routing already honours the region end to end: the coordinator filters online
nodes to the region, weights them by health, and when none match it falls back
to the managed pool *with country targeting for that same region*
(`routing_service.py:_get_brightdata_fallback` appends `-country-<region>`).
A request is therefore never served from a different country.

Sending X-SpaceRouter-Strict-Routing with every region was tried and reverted:
it disables the managed fallback entirely, so `--region US` returned 503
wherever the home-node pool for that country was empty. The AQ report is not a
routing bug — no provider covers Antarctica, so the fallback itself fails — it
is a billing bug, tracked separately.
"""
from __future__ import annotations

import pytest

from spacerouter.client import _build_proxy


def _headers(**kwargs) -> dict:
    proxy = _build_proxy("sr_live_test", "http://gw.test:8080", "http", **kwargs)
    return {k.lower(): v for k, v in proxy.headers.items()}


def test_region_is_sent_without_disabling_the_fallback():
    headers = _headers(region="US", ip_type=None)
    assert headers.get("x-spacerouter-region") == "US"
    assert "x-spacerouter-strict-routing" not in headers, (
        "strict routing disables the country-targeted managed fallback, so a "
        "region with no home node online returns 503 instead of being served"
    )


def test_no_region_sends_no_routing_headers():
    headers = _headers(region=None, ip_type=None)
    assert "x-spacerouter-strict-routing" not in headers
    assert "x-spacerouter-region" not in headers


def test_ip_type_alone_sends_no_strict_routing():
    headers = _headers(region=None, ip_type="mobile")
    assert headers.get("x-spacerouter-ip-type") == "mobile"
    assert "x-spacerouter-strict-routing" not in headers


def test_region_and_ip_type_together_send_no_strict_routing():
    headers = _headers(region="JP", ip_type="mobile")
    assert headers.get("x-spacerouter-region") == "JP"
    assert headers.get("x-spacerouter-ip-type") == "mobile"
    assert "x-spacerouter-strict-routing" not in headers


def test_ip_type_union_matches_the_gateway_valid_set():
    """The gateway's valid set is residential/mobile/business/hosting.

    `datacenter` was in the SDK's IpType and the CLI --help but was rejected
    with 400 Bad Request. It is a fallback tier, not an operator choice.
    `hosting` is accepted by the gateway and was missing from the union.
    """
    import typing

    from spacerouter.models import IpType

    assert set(typing.get_args(IpType)) == {
        "residential", "mobile", "business", "hosting",
    }


@pytest.mark.parametrize("ip_type", ["residential", "mobile", "business", "hosting"])
def test_gateway_accepted_ip_types_pass_validation(ip_type):
    assert _headers(region=None, ip_type=ip_type).get("x-spacerouter-ip-type") == ip_type


@pytest.mark.parametrize("ip_type", ["datacenter", "Residential", "bogus", "dc"])
def test_ip_type_the_gateway_rejects_never_leaves_the_client(ip_type):
    """A 400 round trip is a wasted request; reject it before CONNECT.

    Dropping `datacenter` from the type union alone left the runtime path
    unchanged — the CLI passes `--ip-type` through as a plain string, so
    `--ip-type datacenter` still reached the gateway and still 400'd.
    """
    with pytest.raises(ValueError, match="ip_type must be one of"):
        _headers(region=None, ip_type=ip_type)


def test_client_construction_rejects_a_bad_ip_type():
    from spacerouter import SpaceRouter

    with pytest.raises(ValueError, match="ip_type must be one of"):
        SpaceRouter("sr_live_test", gateway_url="http://gw.test:8080", ip_type="datacenter")
