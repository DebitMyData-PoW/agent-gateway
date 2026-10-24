"""The interactive API docs (/docs, /redoc) and the DebitMyData favicon.

FastAPI's built-in docs pages are switched off in ``create_app`` and served here instead,
so that they carry the DebitMyData logo and open with "Try it out" already enabled.
"""

from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, Response

router = APIRouter(include_in_schema=False)

_STATIC = files("dmd_gateway") / "static"
_ICONS = {
    "favicon.ico": "image/x-icon",
    "favicon.png": "image/png",
}
FAVICON_URL = "/favicon.png"

# Swagger UI options: links to each endpoint (#/<section>/<operation>), "Try it out" already
# on, request durations shown, and the schema list at the bottom collapsed.
SWAGGER_UI_PARAMETERS = {
    "deepLinking": True,
    "tryItOutEnabled": True,
    "displayRequestDuration": True,
    "defaultModelsExpandDepth": 0,
    "docExpansion": "list",
}


@router.get("/docs")
def swagger_ui(request: Request) -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=request.app.openapi_url,
        title=f"{request.app.title} - API docs",
        swagger_favicon_url=FAVICON_URL,
        swagger_ui_parameters=SWAGGER_UI_PARAMETERS,
    )


@router.get("/redoc")
def redoc(request: Request) -> HTMLResponse:
    return get_redoc_html(
        openapi_url=request.app.openapi_url,
        title=f"{request.app.title} - API reference",
        redoc_favicon_url=FAVICON_URL,
        with_google_fonts=False,
    )


def _icon(name: str) -> Response:
    return Response(
        (_STATIC / name).read_bytes(),
        media_type=_ICONS[name],
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.ico")
def favicon_ico() -> Response:
    return _icon("favicon.ico")


@router.get("/favicon.png")
def favicon_png() -> Response:
    return _icon("favicon.png")
