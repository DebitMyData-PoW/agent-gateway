"""The debitmydata.com look shared by the gateway's pages for people: the "Get a DID" page,
the staging guided test page, the advanced API docs (Swagger UI) and the policy pages.

``CSS`` (the Mada font, served by this gateway, and the brand colours) goes at the start of
each page's own inline style, and ``header`` and ``footer`` give every page the same top bar
and footer. The links are the same on every page: "Get a DID" at /get-did, the guided test
(staging only) at /docs, the advanced API docs at /docs/advanced and the policies.
"""

from __future__ import annotations

from importlib.resources import files

CSS = (files("dmd_gateway") / "static" / "brand.css").read_text(encoding="utf-8")

GET_DID_PATH = "/get-did"
GUIDED_PATH = "/docs"  # staging
STAGING_GUIDED_PAGE = "https://staging-api.debitmydata.io/docs"
PRODUCTION_SITE = "https://api.debitmydata.io"
ADVANCED_PATH = "/docs/advanced"
POLICIES_PATH = "/policies"

FOOTER_LINKS = (
    ("/policies/terms", "Terms"),
    ("/policies/privacy", "Privacy"),
    ("/policies/refunds", "Refunds"),
    ("/policies/payment-methods", "Payment methods"),
    ("/policies/did", "DIDs"),
    ("/policies/security", "Security"),
    (ADVANCED_PATH, "API docs (advanced)"),
    ("https://debitmydata.com/", "debitmydata.com"),
)


def _pages(staging: bool, get_did: str = GET_DID_PATH) -> list[tuple[str, str, str]]:
    pages = [("get-did", get_did, "Get a DID")]
    if staging:
        pages.append(("guided", GUIDED_PATH, "Guided test"))
    pages += [
        ("advanced", ADVANCED_PATH, "Advanced: API docs"),
        ("policies", POLICIES_PATH, "Policies"),
    ]
    return pages


# On staging, the same page on the real (production) server; the guided test has none.
_PRODUCTION_PAGE = {
    "get-did": GET_DID_PATH,
    "guided": GET_DID_PATH,
    "advanced": ADVANCED_PATH,
    "policies": POLICIES_PATH,
}


def header(current: str, staging: bool, get_did: str = GET_DID_PATH) -> str:
    """The top bar: the DebitMyData name (to debitmydata.com) and the gateway's pages, with
    ``current`` (get-did, guided, advanced or policies) marked; on staging, a link to the
    same page on the real API. ``get_did`` is where "Get a DID" is (the installable app
    serves it at its root)."""
    links = "".join(
        f'<a class=on aria-current=page href="{path}">{label}</a>'
        if key == current
        else f'<a href="{path}">{label}</a>'
        for key, path, label in _pages(staging, get_did)
    )
    if staging:
        page = _PRODUCTION_PAGE.get(current, GET_DID_PATH)
        links += (
            f'<a class=real href="{PRODUCTION_SITE}{page}" title="The same page on the real API, '
            'api.debitmydata.io (production)">Real API ↗</a>'
        )
    return (
        '<header class="topbar"><div class=in>'
        '<a class=brand href="https://debitmydata.com/"><img src="/favicon.png" alt="">'
        f'DebitMyData</a><nav class=modes aria-label="Gateway pages">{links}</nav>'
        "</div></header>"
    )


def footer() -> str:
    links = "".join(f'<a href="{path}">{label}</a>' for path, label in FOOTER_LINKS)
    return (
        f'<footer class="site-footer"><div class=in>{links}'
        '<span>Questions: <a href="mailto:info@debitmydata.com">info@debitmydata.com</a>'
        "</span></div></footer>"
    )
