"""Headers every answer carries, whatever made it (the API, the pages, the MCP and A2A servers,
the sign-in server, errors):

* ``Strict-Transport-Security`` (HSTS, RFC 6797), when the gateway's address is https: a
  browser that has been here once uses https only for this name, for a year. Not for its
  subdomains (no ``includeSubDomains``): other DebitMyData sites decide for themselves;
* ``X-Content-Type-Options: nosniff``: a browser takes each answer as the type it says it is,
  never as a script or a page it guessed.

An answer that already sets one of them keeps its own.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HSTS = "max-age=31536000"


class SecurityHeaders:
    def __init__(self, app: ASGIApp, *, https: bool) -> None:
        self.app = app
        self.headers: list[tuple[bytes, bytes]] = [(b"x-content-type-options", b"nosniff")]
        if https:
            self.headers.append((b"strict-transport-security", HSTS.encode()))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(name, value) for name, value in self.headers if name not in present]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
