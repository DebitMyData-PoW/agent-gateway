"""Which look the "Get a DID-LLM" page has: "classic" (the debitmydata.com look), "fintech" or
"aurora", the two new DebitMyData designs (the same ones as the debitmydata.io website).

The ``page_design`` setting chooses it for every visitor (classic unless set). To compare the
designs on the real page before choosing, previews can be switched on (``page_design_preview``,
always on staging): a visitor then opens the page with ``?design=fintech`` (or aurora, or
classic) and keeps that look in a cookie, also after coming back from Stripe's or PayPal's
payment page.

A design is a stylesheet added after the classic one (static/design-<name>.css): it changes
the colours, the fonts and the layout, so every part of the page, the payment steps included,
keeps working as before. Its fonts are served by this gateway (SIL Open Font License, licence
texts next to them in static/fonts).
"""

from __future__ import annotations

from importlib.resources import files

from fastapi import Request

CLASSIC = "classic"
DESIGNS = (CLASSIC, "fintech", "aurora")
PREVIEW_PARAMETER = "design"
PREVIEW_COOKIE = "dmd-design-id"
PREVIEW_COOKIE_DAYS = 30

_STATIC = files("dmd_gateway") / "static"

# Each design's fonts (variable fonts: one file has every weight).
FONTS: dict[str, tuple[str, ...]] = {
    CLASSIC: (),
    "fintech": ("plus-jakarta-sans-wght.woff2", "jetbrains-mono-wght.woff2"),
    "aurora": ("sora-wght.woff2", "inter-wght.woff2", "jetbrains-mono-wght.woff2"),
}
ALL_FONTS = tuple(sorted({name for names in FONTS.values() for name in names}))


def stylesheet(design: str) -> str:
    """The design's own stylesheet, added after the classic one ("" for the classic look)."""
    if design == CLASSIC:
        return ""
    return (_STATIC / f"design-{design}.css").read_text(encoding="utf-8")


def previews_allowed(settings: object) -> bool:
    return bool(
        getattr(settings, "page_design_preview", False)
        or getattr(settings, "staging_test_tools", False)
    )


def chosen(request: Request) -> tuple[str, str | None]:
    """The design for this visit, and the value to keep in the preview cookie (None: no change).

    Without previews, always the configured one. With previews, ``?design=`` wins, then the
    cookie, then the configured one."""
    settings = request.app.state.settings
    configured = getattr(settings, "page_design", CLASSIC)
    if not previews_allowed(settings):
        return configured, None
    asked = request.query_params.get(PREVIEW_PARAMETER, "").strip().lower()
    if asked in DESIGNS:
        return asked, asked
    kept = request.cookies.get(PREVIEW_COOKIE, "")
    if kept in DESIGNS:
        return kept, None
    return configured, None


def keep(response: object, design: str, secure: bool) -> None:
    """Remember a previewed design in this site's own cookie."""
    response.set_cookie(  # type: ignore[attr-defined]
        PREVIEW_COOKIE,
        design,
        max_age=PREVIEW_COOKIE_DAYS * 86400,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
