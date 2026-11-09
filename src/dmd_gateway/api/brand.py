"""The debitmydata.com look shared by the gateway's pages for people: the "Get a DID" page,
the staging guided test page, the advanced API docs (Swagger UI) and the policy pages.

``CSS`` (the Mada font, served by this gateway, and the brand colours) goes at the start of
each page's own inline style, and ``header`` and ``footer`` give every page the same top bar
and footer. The links are the same on every page: "Get a DID" at /get-did, the guided test
(staging only) at /docs, the advanced API docs at /docs/advanced and the policies. On a
phone the top bar's links fold into a menu button; it needs no script (a checkbox and its
label), so it works under every page's Content-Security-Policy. The "back to top" button is
a plain link too: it fades in as the page scrolls down (CSS scroll-driven animation), and
browsers without that show it all the time.
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

HOME_PATH = "/"  # the site root: the Get a DID page (the guided test on staging)
SOURCE_CODE = "https://github.com/DebitMyData-PoW/agent-gateway"
CONTACT = "info@debitmydata.com"
POLICY_LINKS = (
    ("/policies/terms", "Terms"),
    ("/policies/privacy", "Privacy"),
    ("/policies/refunds", "Refunds"),
    ("/policies/payment-methods", "Payment methods"),
    ("/policies/did", "DIDs"),
    ("/policies/security", "Security"),
)
# DebitMyData's other sites, in the footer.
SITE_LINKS = (
    ("https://debitmydata.com/", "debitmydata.com"),
    ("https://agenticssocialspaces.debitmydata.com/", "Agentic Spaces"),
    ("https://debitmydata.io/", "DMD coin"),
    ("https://mempool.debitmydata.io/", "Explorer"),
    ("https://pool.debitmydata.io/", "Mining pool"),
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
    """The top bar: the DebitMyData name (to this site's home) and the gateway's pages, with
    ``current`` (get-did, guided, advanced or policies) marked; on staging, a link to the
    same page on the real API. ``get_did`` is where "Get a DID" is (the installable app
    serves it at its root). On a phone the pages fold into the menu button."""
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
        f'<a class=brand href="{HOME_PATH}"><img src="/favicon.png" alt="">DebitMyData</a>'
        '<input type=checkbox id=menu-toggle class=menu-toggle aria-label="Menu" '
        "aria-controls=site-menu>"
        "<label for=menu-toggle class=menu-button aria-hidden=true><span></span></label>"
        f'<nav id=site-menu class=modes aria-label="Gateway pages">{links}</nav>'
        "</div></header>"
    )


def _column(title: str, links: list[tuple[str, str]]) -> str:
    items = "".join(f'<a href="{path}">{label}</a>' for path, label in links)
    return f"<div class=col><p class=head>{title}</p>{items}</div>"


def footer(staging: bool = False, get_did: str = GET_DID_PATH) -> str:
    """The footer, in columns like the Agentic Spaces site: the gateway's pages, the
    developers' links, the policies, DebitMyData's other sites and the contact address."""
    pages = [(get_did, "Get a DID")]
    if staging:
        pages.append((GUIDED_PATH, "Guided test"))
    pages.append((POLICIES_PATH, "All policies"))
    developers = [(ADVANCED_PATH, "API docs"), (SOURCE_CODE, "Source code")]
    columns = (
        _column("Get a DID", pages)
        + _column("Developers", developers)
        + _column("Policies", list(POLICY_LINKS))
        + _column("DebitMyData", list(SITE_LINKS))
        + _column("Contact", [(f"mailto:{CONTACT}", CONTACT)])
    )
    return (
        f'<footer class="site-footer"><div class=in><div class=cols>{columns}</div>'
        "<p class=legal>DebitMyData Agent Gateway · DIDs and payments for agents and people"
        f"</p></div></footer>{TO_TOP}"
    )


# Fixed at the bottom right; "#top" scrolls to the top of the page.
TO_TOP = (
    '<a class=to-top href="#top" aria-label="Back to top" title="Back to top">'
    '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false">'
    '<path d="M12 5l-7 7m7-7l7 7M12 5v14" fill="none" stroke="currentColor" stroke-width="2.4" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg></a>'
)
