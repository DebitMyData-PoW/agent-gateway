"""One address for people, one for agents and developers.

In production the same gateway answers at two names (nginx sends both to it): the API's,
api.debitmydata.io (``public_base_url``), for agents and developers, and the app's,
id.debitmydata.io (the ``app_host`` setting), for people. The pages for people (Get a DID-LLM,
the directory, the policies) lead from the API's name to the app's; the API docs lead from the
app's name to the API's. So every page has one address that people share and bookmark, and the
installed app keeps people inside it. The API itself, llms.txt and the files agents read
(/v1/..., /policies/<name>.md, /llms.txt, the MCP server) answer at both names, unmoved.

Without an app host (staging, development) nothing moves.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse


def is_app(request: Request) -> bool:
    """Whether the request came to the app's host (nginx passes the visitor's Host)."""
    host = request.app.state.settings.app_host
    return bool(host) and request.headers.get("host", "").strip().lower() == host


def _query(request: Request) -> str:
    return f"?{request.url.query}" if request.url.query else ""


def to_app(request: Request, path: str | None = None) -> RedirectResponse | None:
    """A page for people, reached at the API's name: leads to the app's (same query).
    ``path`` is the page's path there, when it is not the same one."""
    host = request.app.state.settings.app_host
    if not host or is_app(request):
        return None
    target = path if path is not None else request.url.path
    return RedirectResponse(f"https://{host}{target}{_query(request)}", status_code=308)


def to_api(request: Request) -> RedirectResponse | None:
    """A page for developers (the API docs), reached at the app's name: leads to the API's."""
    if not is_app(request):
        return None
    base = request.app.state.settings.public_base_url.rstrip("/")
    return RedirectResponse(f"{base}{request.url.path}{_query(request)}", status_code=308)
