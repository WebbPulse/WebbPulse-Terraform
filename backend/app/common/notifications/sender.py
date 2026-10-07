"""The one HTTP POST a notification delivery makes, guarded against reaching inside.

A generic webhook URL is chosen by whoever configures it, so the sender resolves the
host itself, refuses any address that is not globally routable, and connects to the
address it checked with the original host as the TLS server name and `Host` header.
A DNS answer that changes between the check and the connect cannot redirect the
request, and redirects are never followed.

No error raised here or recorded from here quotes the URL, since its path is the
credential for Slack and Discord.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Final, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

TIMEOUT_SECONDS: Final = 10.0
"""Slack and Discord answer in well under a second; a slow generic receiver gets ten."""

EXCERPT_CHARACTERS: Final = 512
"""How much of a receiver's answer is kept to show beside the last delivery."""

READ_LIMIT_BYTES: Final = 4096

USER_AGENT: Final = "WebbPulse-Terraform-Notifications/1.0"

BLOCKED_ADDRESS: Final = "blocked_address"
DNS_FAILED: Final = "dns_failed"
TIMEOUT: Final = "timeout"
CONNECTION_FAILED: Final = "connection_failed"
TLS_ERROR: Final = "tls_error"
REDIRECT_REFUSED: Final = "redirect_refused"
INVALID_URL: Final = "invalid_url"
HTTP_ERROR: Final = "http_error"

RETRYABLE_ERRORS: Final = frozenset({DNS_FAILED, TIMEOUT, CONNECTION_FAILED, TLS_ERROR})
"""Faults a later attempt may not hit. A blocked address or a refused redirect will recur."""


@dataclass(frozen=True)
class SendResult:
    """The outcome of one POST: the receiver's status, or the category of what failed."""

    status_code: int | None
    error: str | None = None
    response_excerpt: str | None = None
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        """Whether the receiver accepted the delivery."""
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def retryable(self) -> bool:
        """Whether a later attempt could succeed: a transient fault, a 429 or a 5xx."""
        if self.status_code is None:
            return self.error in RETRYABLE_ERRORS
        return self.status_code == 429 or self.status_code >= 500


def resolve(host: str, port: int) -> list[str]:
    """Every address the host resolves to. Patched in tests."""
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(answer[4][0]) for answer in answers})


def _transport() -> httpx.BaseTransport | None:
    """The transport the client sends through. Patched in tests."""
    return None


def _retry_after(value: str | None) -> int | None:
    """Seconds from a `Retry-After` header in either of its two forms, or `None`."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    try:
        delta = parsedate_to_datetime(text) - datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0, int(delta.total_seconds()))


def _pinned_address(host: str, port: int) -> str | SendResult:
    """The one checked address to connect to, or the refusal as a result."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = resolve(host, port)
        except (OSError, UnicodeError):
            return SendResult(status_code=None, error=DNS_FAILED)
    if not addresses:
        return SendResult(status_code=None, error=DNS_FAILED)
    for address in addresses:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
        if not parsed.is_global or parsed.is_multicast:
            return SendResult(status_code=None, error=BLOCKED_ADDRESS)
    return addresses[0]


def post(url: str, body: bytes, headers: Mapping[str, str]) -> SendResult:
    """POST `body` to `url` and report what happened, never raising for a network fault."""
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError:
        return SendResult(status_code=None, error=INVALID_URL)
    host = parts.hostname
    if parts.scheme != "https" or not host:
        return SendResult(status_code=None, error=INVALID_URL)
    pinned = _pinned_address(host, port)
    if isinstance(pinned, SendResult):
        return pinned
    netloc = f"[{pinned}]" if ":" in pinned else pinned
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    target = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    host_header = host if not parts.port else f"{host}:{parts.port}"
    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **headers,
        "Host": host_header,
    }
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=_transport()) as client:
            request = client.build_request(
                "POST", target, content=body, headers=request_headers, extensions={"sni_hostname": host}
            )
            response = client.send(request, stream=True)
            try:
                raw = b""
                for chunk in response.iter_bytes():
                    raw += chunk
                    if len(raw) >= READ_LIMIT_BYTES:
                        break
            finally:
                response.close()
    except httpx.TimeoutException:
        return SendResult(status_code=None, error=TIMEOUT)
    except httpx.ConnectError as error:
        cause = error.__cause__ or error.__context__
        if isinstance(cause, ssl.SSLError) or "SSL" in type(cause).__name__ or "certificate" in str(error).lower():
            return SendResult(status_code=None, error=TLS_ERROR)
        return SendResult(status_code=None, error=CONNECTION_FAILED)
    except httpx.HTTPError:
        return SendResult(status_code=None, error=CONNECTION_FAILED)
    excerpt = raw.decode("utf-8", errors="replace")[:EXCERPT_CHARACTERS].strip() or None
    if 300 <= response.status_code < 400:
        return SendResult(status_code=response.status_code, error=REDIRECT_REFUSED, response_excerpt=None)
    error = None if 200 <= response.status_code < 300 else HTTP_ERROR
    return SendResult(
        status_code=response.status_code,
        error=error,
        response_excerpt=excerpt,
        retry_after=_retry_after(response.headers.get("retry-after")),
    )


__all__ = [
    "BLOCKED_ADDRESS",
    "CONNECTION_FAILED",
    "DNS_FAILED",
    "HTTP_ERROR",
    "INVALID_URL",
    "REDIRECT_REFUSED",
    "TIMEOUT",
    "TLS_ERROR",
    "SendResult",
    "post",
    "resolve",
]
