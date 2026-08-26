"""v1.5.2 QA #2: SpaceRouter metadata must survive the proxy CONNECT.

For ``http://`` targets the gateway injects ``X-SpaceRouter-Node`` /
``-Routing`` / ``-Request-Id`` into the inner response, so reading them
off ``httpx.Response.headers`` works. For ``https://`` targets the inner
exchange is end-to-end TLS and the gateway can only stamp those headers
on the ``CONNECT 200`` response, which httpcore consumes and discards
(``httpcore/_sync/http_proxy.py`` ``TunnelHTTPConnection.handle_request``).
QA confirmed the gateway sends all three and the JS SDK surfaces them,
while the Python SDK reported ``request_id=None`` and had no node /
routing accessors at all.

These tests drive a real local CONNECT proxy and a real local TLS
target. A mocked tunnel cannot reproduce the defect: the whole bug is
that a genuine CONNECT round-trip throws the headers away before any
Response object the SDK can see exists.
"""

from __future__ import annotations

import http.server
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from spacerouter import AsyncSpaceRouter, RateLimitError, SpaceRouter

NODE_ID = "node-7f3a"
ROUTING_TAG = "home"
REQUEST_ID = "req-abc-123"

CONNECT_RESPONSE = (
    "HTTP/1.1 200 Connection established\r\n"
    f"X-SpaceRouter-Node: {NODE_ID}\r\n"
    f"X-SpaceRouter-Routing: {ROUTING_TAG}\r\n"
    f"X-SpaceRouter-Request-Id: {REQUEST_ID}\r\n"
    "\r\n"
).encode()

TARGET_BODY = b'{"origin": "203.0.113.7"}'


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _TargetHandler(http.server.BaseHTTPRequestHandler):
    """Origin server. Deliberately emits no SpaceRouter headers.

    Anything the SDK reports for an HTTPS target therefore has to have
    come off the CONNECT response, not the tunnelled response.
    """

    protocol_version = "HTTP/1.1"
    status = 200

    def do_GET(self) -> None:  # noqa: N802 — http.server contract
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(TARGET_BODY)))
        self.end_headers()
        self.wfile.write(TARGET_BODY)

    def log_message(self, *_a: object, **_kw: object) -> None:
        return


class _TunnelProxyHandler(socketserver.StreamRequestHandler):
    """Answers CONNECT with the gateway's metadata headers, then pipes."""

    rbufsize = 0
    connect_response = CONNECT_RESPONSE

    def handle(self) -> None:
        request_line = self.rfile.readline()
        if not request_line:
            return
        while True:
            line = self.rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        parts = request_line.decode("latin-1").split()
        if len(parts) < 2 or parts[0] != "CONNECT":
            self.connection.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, _, port = parts[1].rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)))
        except OSError:
            self.connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        self.connection.sendall(self.connect_response)
        _pipe(self.connection, upstream)

    def handle_error(self, *_a: object, **_kw: object) -> None:
        return


class _ForwardProxyHandler(socketserver.StreamRequestHandler):
    """Serves absolute-URI GETs directly, stamping the gateway headers.

    Mirrors what the gateway does for plain-HTTP targets: no tunnel, so
    the metadata rides the inner response.
    """

    rbufsize = 0

    def handle(self) -> None:
        request_line = self.rfile.readline()
        if not request_line:
            return
        while True:
            line = self.rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        self.connection.sendall(
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(TARGET_BODY)}\r\n"
            f"X-SpaceRouter-Node: {NODE_ID}\r\n"
            f"X-SpaceRouter-Routing: {ROUTING_TAG}\r\n"
            f"X-SpaceRouter-Request-Id: {REQUEST_ID}\r\n"
            "Connection: close\r\n"
            "\r\n".encode("latin-1")
            + TARGET_BODY,
        )

    def handle_error(self, *_a: object, **_kw: object) -> None:
        return


def _pipe(left: socket.socket, right: socket.socket) -> None:
    def copy(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    forward = threading.Thread(target=copy, args=(left, right), daemon=True)
    forward.start()
    copy(right, left)
    forward.join(timeout=5)
    right.close()


def _serve(server: socketserver.BaseServer) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


@pytest.fixture(scope="session")
def self_signed_cert(tmp_path_factory) -> Path:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required to build the local TLS target")
    pem = tmp_path_factory.mktemp("tls") / "target.pem"
    key = pem.with_suffix(".key")
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(pem),
            "-days", "3650", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    pem.write_bytes(pem.read_bytes() + key.read_bytes())
    return pem


def _start_https_target(cert: Path, handler) -> tuple[_ThreadedHTTPServer, str]:
    port = _free_port()
    server = _ThreadedHTTPServer(("127.0.0.1", port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    _serve(server)
    return server, f"https://127.0.0.1:{port}/ip"


@pytest.fixture
def https_target(self_signed_cert):
    server, url = _start_https_target(self_signed_cert, _TargetHandler)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def rate_limited_https_target(self_signed_cert):
    handler = type("_RateLimited", (_TargetHandler,), {"status": 429})
    server, url = _start_https_target(self_signed_cert, handler)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def tunnel_proxy():
    port = _free_port()
    server = _ThreadedTCPServer(("127.0.0.1", port), _TunnelProxyHandler)
    _serve(server)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def forward_proxy():
    port = _free_port()
    server = _ThreadedTCPServer(("127.0.0.1", port), _ForwardProxyHandler)
    _serve(server)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# HTTPS targets — metadata only exists on the CONNECT response
# ---------------------------------------------------------------------------


def test_sync_https_target_exposes_connect_metadata(tunnel_proxy, https_target):
    with SpaceRouter(
        "sr_live_test", gateway_url=tunnel_proxy, verify=False,
    ) as client:
        resp = client.get(https_target)

    assert resp.status_code == 200
    assert "x-spacerouter-node" not in resp.headers
    assert resp.request_id == REQUEST_ID
    assert resp.node_id == NODE_ID
    assert resp.routing_tag == ROUTING_TAG


async def test_async_https_target_exposes_connect_metadata(tunnel_proxy, https_target):
    async with AsyncSpaceRouter(
        "sr_live_test", gateway_url=tunnel_proxy, verify=False,
    ) as client:
        resp = await client.get(https_target)

    assert resp.status_code == 200
    assert "x-spacerouter-node" not in resp.headers
    assert resp.request_id == REQUEST_ID
    assert resp.node_id == NODE_ID
    assert resp.routing_tag == ROUTING_TAG


def test_sync_https_metadata_survives_tunnel_reuse(tunnel_proxy, https_target):
    with SpaceRouter(
        "sr_live_test", gateway_url=tunnel_proxy, verify=False,
    ) as client:
        client.get(https_target)
        resp = client.get(https_target)

    assert resp.node_id == NODE_ID
    assert resp.request_id == REQUEST_ID


# ---------------------------------------------------------------------------
# HTTP targets — metadata rides the inner response (must keep working)
# ---------------------------------------------------------------------------


def test_sync_http_target_exposes_inner_response_metadata(forward_proxy):
    with SpaceRouter("sr_live_test", gateway_url=forward_proxy) as client:
        resp = client.get("http://example.invalid/ip")

    assert resp.status_code == 200
    assert resp.request_id == REQUEST_ID
    assert resp.node_id == NODE_ID
    assert resp.routing_tag == ROUTING_TAG


async def test_async_http_target_exposes_inner_response_metadata(forward_proxy):
    async with AsyncSpaceRouter("sr_live_test", gateway_url=forward_proxy) as client:
        resp = await client.get("http://example.invalid/ip")

    assert resp.status_code == 200
    assert resp.request_id == REQUEST_ID
    assert resp.node_id == NODE_ID
    assert resp.routing_tag == ROUTING_TAG


def test_metadata_is_none_when_gateway_sends_nothing(https_target):
    port = _free_port()

    class _BareTunnelHandler(_TunnelProxyHandler):
        connect_response = b"HTTP/1.1 200 Connection established\r\n\r\n"

    server = _ThreadedTCPServer(("127.0.0.1", port), _BareTunnelHandler)
    _serve(server)
    try:
        with SpaceRouter(
            "sr_live_test", gateway_url=f"http://127.0.0.1:{port}", verify=False,
        ) as client:
            resp = client.get(https_target)
        assert resp.status_code == 200
        assert resp.request_id is None
        assert resp.node_id is None
        assert resp.routing_tag is None
    finally:
        server.shutdown()
        server.server_close()


def test_typed_error_over_tunnel_carries_connect_request_id(
    tunnel_proxy, rate_limited_https_target,
):
    with SpaceRouter(
        "sr_live_test", gateway_url=tunnel_proxy, verify=False,
    ) as client:
        with pytest.raises(RateLimitError) as exc:
            client.get(rate_limited_https_target)

    assert exc.value.request_id == REQUEST_ID
