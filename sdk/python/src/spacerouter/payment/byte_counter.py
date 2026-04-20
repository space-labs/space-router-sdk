"""Byte counting helpers for SpaceRouter SPACE payment flow.

The gateway signs off on ``dataAmount`` based on what it relayed. Consumers
using SPACE payments should count bytes locally and compare against the
gateway's claim before signing a receipt, so a misbehaving gateway can't
inflate the bill.

These helpers cover the common httpx entry points. Use them to wrap a
``ProxyResponse`` and then pass ``bc.total`` to
``SpaceRouterSPACE.validate_receipt(..., observed_bytes=bc.total)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Iterator


@dataclass
class ByteCount:
    """Cumulative byte tracker for one proxied request/response pair."""

    request_bytes: int = 0
    response_bytes: int = 0

    @property
    def total(self) -> int:
        return self.request_bytes + self.response_bytes

    def add_request(self, n: int) -> None:
        self.request_bytes += n

    def add_response(self, n: int) -> None:
        self.response_bytes += n


def count_request_bytes(method: str, url: str, content: bytes | str | None, headers: dict | None) -> int:
    """Estimate outbound bytes for a given request.

    Approximates HTTP/1.1 request framing: ``METHOD URL HTTP/1.1\\r\\n`` +
    headers + CRLF + body. Close enough for tolerance-based validation.
    """
    head = f"{method.upper()} {url} HTTP/1.1\r\n".encode("latin-1", errors="replace")
    if headers:
        head += b"".join(
            f"{k}: {v}\r\n".encode("latin-1", errors="replace")
            for k, v in headers.items()
        )
    head += b"\r\n"
    if content is None:
        body_len = 0
    elif isinstance(content, str):
        body_len = len(content.encode("utf-8"))
    else:
        body_len = len(content)
    return len(head) + body_len


def count_response_bytes(response) -> int:
    """Count inbound bytes for a non-streamed httpx.Response.

    Includes status line + headers + body. Call AFTER the response has
    been materialised via ``.content`` / ``.text`` / ``.read()``.
    """
    status_line = f"HTTP/{response.http_version} {response.status_code} {response.reason_phrase}\r\n".encode("latin-1", errors="replace")
    headers = b"".join(
        f"{k}: {v}\r\n".encode("latin-1", errors="replace")
        for k, v in response.headers.items()
    )
    body = response.content or b""
    return len(status_line) + len(headers) + len(b"\r\n") + len(body)


def iter_and_count(chunks: Iterator[bytes], counter: ByteCount) -> Iterator[bytes]:
    """Wrap a sync iterator of chunks, adding each chunk's length to ``counter``."""
    for chunk in chunks:
        counter.add_response(len(chunk))
        yield chunk


async def aiter_and_count(
    chunks: AsyncIterator[bytes],
    counter: ByteCount,
) -> AsyncIterator[bytes]:
    """Wrap an async iterator of chunks, adding each chunk's length to ``counter``."""
    async for chunk in chunks:
        counter.add_response(len(chunk))
        yield chunk
