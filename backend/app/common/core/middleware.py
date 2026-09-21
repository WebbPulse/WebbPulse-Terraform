"""ASGI middleware both composition roots share: slash tolerance and a domain header.

Portfolio's equivalent also carries first-request seeding. This project seeds
nothing: a workspace, a variable and a run are all created by a caller, so there
is no singleton row to plant on the first request.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Final

from starlette.routing import Match, Router

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

DOMAIN_HEADER: Final = "x-webbpulse-domain"
"""Names the application that served a request, so which function answered is
readable from the response rather than from CloudWatch."""

MONOLITH_DOMAIN: Final = "monolith"
"""What the whole-surface root reports, since no single domain name is true of it."""


class TrailingSlashMiddleware:
    """Serve a path whose trailing slash does not match any declared route."""

    def __init__(self, app: ASGIApp, router: Router) -> None:
        """Wrap `app`, matching candidate paths against `router`."""
        self.app = app
        self.router = router

    def _matches(self, scope: Scope) -> bool:
        """Whether any route in the router matches this scope's path."""
        for route in self.router.routes:
            match, _ = route.matches(scope)
            if match != Match.NONE:
                return True
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Rewrite the scope's path to its matching alternate, then pass it on."""
        if scope["type"] == "http" and not self._matches(scope):
            path = str(scope["path"])
            alternate = path[:-1] if path.endswith("/") and path != "/" else path + "/"
            if self._matches({**scope, "path": alternate}):
                scope["path"] = alternate
                scope["raw_path"] = alternate.encode("utf-8")
        await self.app(scope, receive, send)


class DomainHeaderMiddleware:
    """Stamp every response with the name of the application that produced it.

    Written on `http.response.start`, so it lands on the error envelopes too.
    """

    def __init__(self, app: ASGIApp, domain: str) -> None:
        """Wrap `app`, stamping responses with `domain`."""
        self.app = app
        self.value = domain.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add the domain header to the outgoing response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            """Append the domain header as the response starts."""
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((DOMAIN_HEADER.encode("latin-1"), self.value))
            await send(message)

        await self.app(scope, receive, send_wrapper)


__all__ = [
    "DOMAIN_HEADER",
    "MONOLITH_DOMAIN",
    "DomainHeaderMiddleware",
    "TrailingSlashMiddleware",
]
