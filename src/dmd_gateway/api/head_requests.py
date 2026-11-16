"""HEAD requests (RFC 9110, section 9.3.2): the same answer as GET, without its body.

FastAPI answers HEAD only where a route declares it, so most addresses answered 405 to HEAD:
the pages, ``/readyz``, ``/llms.txt``, security.txt. Uptime services, link checkers and some
crawlers ask with HEAD (``curl -I`` too). Here, a HEAD that the routes refuse with 405 is
answered as a GET would be, headers and status alike, with no body. Addresses that answer
HEAD themselves keep their own answer, and an address without GET still answers 405.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class HeadAsGet:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return

        refused = False

        async def send_unless_refused(message: Message) -> None:
            nonlocal refused
            if message["type"] == "http.response.start" and message["status"] == 405:
                refused = True
            if not refused:
                await send(message)

        await self.app(scope, receive, send_unless_refused)
        if not refused:
            return

        asked = False

        async def receive_no_body() -> Message:
            nonlocal asked
            if not asked:
                asked = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return await receive()

        async def send_without_body(message: Message) -> None:
            if message["type"] == "http.response.body":
                if message.get("more_body", False):
                    return
                message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)

        # A copy: the server still sees HEAD in its own scope, and sends no body either.
        await self.app({**scope, "method": "GET"}, receive_no_body, send_without_body)
