"""The notification sender's address choice for dual-stack receivers."""

from __future__ import annotations

import httpx
import pytest

from app.common.notifications import sender

URL = "https://receiver.example.com/hook?token=t"
IPV6 = "2600:1f14:50b:9a01::1"
IPV4_HIGH = "52.42.98.59"
IPV4_LOW = "44.229.211.1"


def _transport(monkeypatch: pytest.MonkeyPatch, unreachable: set[str]) -> list[str]:
    """Answer 200 from every address except `unreachable`, recording the hosts tried."""
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Refuse the connection to an unreachable address, else answer."""
        tried.append(request.url.host)
        if request.url.host in unreachable:
            raise httpx.ConnectError("[Errno 97] Address family not supported by protocol", request=request)
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(sender, "_transport", lambda: httpx.MockTransport(handler))
    return tried


def test_ipv4_is_tried_before_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dual-stack answer connects over IPv4 first, whatever the string order."""
    monkeypatch.setattr(sender, "resolve", lambda host, port: sorted([IPV6, IPV4_HIGH, IPV4_LOW]))
    tried = _transport(monkeypatch, unreachable={IPV6})

    result = sender.post(URL, b"{}", {})

    assert result.ok
    assert tried == [IPV4_LOW]


def test_a_refused_connection_falls_through_to_the_next_checked_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """When one address cannot be reached the next checked one is tried."""
    monkeypatch.setattr(sender, "resolve", lambda host, port: [IPV4_LOW, IPV6])
    tried = _transport(monkeypatch, unreachable={IPV4_LOW})

    result = sender.post(URL, b"{}", {})

    assert result.ok
    assert tried == [IPV4_LOW, IPV6]


def test_every_address_unreachable_is_a_retryable_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no address reachable the outcome is `connection_failed`, which a retry may clear."""
    monkeypatch.setattr(sender, "resolve", lambda host, port: [IPV4_LOW, IPV6])
    tried = _transport(monkeypatch, unreachable={IPV4_LOW, IPV6})

    result = sender.post(URL, b"{}", {})

    assert result.status_code is None and result.error == sender.CONNECTION_FAILED and result.retryable
    assert tried == [IPV4_LOW, IPV6]


def test_one_private_address_blocks_the_whole_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A private address anywhere in the answer refuses the delivery before any connection."""
    monkeypatch.setattr(sender, "resolve", lambda host, port: [IPV4_LOW, "10.0.0.5"])
    tried = _transport(monkeypatch, unreachable=set())

    result = sender.post(URL, b"{}", {})

    assert result.error == sender.BLOCKED_ADDRESS
    assert tried == []


def test_the_host_header_names_the_receiver_not_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned address carries the original host, so the receiver routes it."""
    monkeypatch.setattr(sender, "resolve", lambda host, port: [IPV4_LOW])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request."""
        seen.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(sender, "_transport", lambda: httpx.MockTransport(handler))

    assert sender.post(URL, b"{}", {}).ok
    assert seen[0].headers["host"] == "receiver.example.com"
    assert seen[0].url.query == b"token=t"
