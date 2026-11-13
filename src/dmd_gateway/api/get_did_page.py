"""The "Get a DID-LLM" page: a DID-LLM registration for people, in the debitmydata.com look.

It takes a visitor through the registration's steps (details and quote, payment, proof of
control, the DID-LLM), filling each one in from the previous answers. The visitor pays from their
own wallet and signs one message with it (EIP-6963 browser wallets, or a signature pasted from
another wallet or the agent's own tools). The page never asks for a private key or a recovery
phrase and never sends money. It keeps the registration's IDs in the browser so that a reload
continues where it stopped.

For a DMD payment, step 2 also shows the payment as a QR code and an "Open in my DMD wallet"
button: the payment link ``debitmydata:ADDRESS?amount=AMOUNT``, the link DMD wallets open
(DMD Core registers that prefix with the system and reads the address and the amount from it).

It is at /get-did on every server, the address to link from DebitMyData's sites; on
production the docs address (/docs) and the site root lead there too, and the Swagger UI page
is the advanced option (/docs/advanced). On staging it adds a "Pay with test coins" button.
Only its own script and style run (hashes in the Content-Security-Policy; the small light / dark
theme script, ``brand.THEME_SCRIPT``, is allowed the same way), its font is served
here (Mada, SIL Open Font License; the look is shared with the other pages, ``brand``), and
it may only call this server.

The installable app: on the host named by the ``app_host`` setting (id.debitmydata.io), the
same page is served at the root, with a web app manifest, icons and a small service worker, so
that people can add it to their phone's home screen and open it full screen, like an app (iPhone:
Share, Add to Home Screen; Android: Install app). The service worker only keeps the last copy
of the page, its font and icons, for opening without a network: it never touches the API calls.
The app's identity is its address, so the host must not change once people have installed it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from importlib.resources import files

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dmd_gateway.api import brand, docs_pages
from dmd_gateway.api.hosts import is_app, to_api, to_app
from dmd_gateway.domain.routes import STRIPE_CHECKOUT
from dmd_gateway.services.quotes import DID_REGISTRATION
from dmd_gateway.settings import Settings

router = APIRouter(include_in_schema=False)  # every server
production_router = APIRouter(include_in_schema=False)  # outside staging: /docs leads here

PAGE_PATH = brand.GET_DID_PATH
DOCS_PATH = "/docs"
ADVANCED_PATH = brand.ADVANCED_PATH
MCP_PATH = brand.MCP_PATH  # the MCP server, the Agentic Superhighway
_STATIC = files("dmd_gateway") / "static"
_STYLE = brand.CSS + (_STATIC / "get-did.css").read_text(encoding="utf-8")
# The QR code encoder (qr.js) runs first: step 2 draws the DMD payment link with it.
_SCRIPT = "\n".join(
    (_STATIC / name).read_text(encoding="utf-8") for name in ("qr.js", "get-did.js")
)
FONTS = (
    "mada-latin-400-normal.woff2",
    "mada-latin-600-normal.woff2",
    "mada-latin-700-normal.woff2",
    "mada-latin-800-normal.woff2",  # the headlines
)


# The installable app (on the app host only).
APP_ROOT = "/"
MANIFEST_PATH = "/manifest.webmanifest"
WORKER_PATH = "/sw.js"
APP_NAME = "DID-LLM DebitMyData"  # "DID-LLM" first: what people see under the icon
APP_SHORT_NAME = "DID-LLM DebitMyData"
THEME_COLOR = "#640000"  # the top bar's red
APP_ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png")
# Raised whenever the icons change, so installed apps and browsers fetch the new ones.
ICON_VERSION = "3"
DESCRIPTION = (
    "Get your DebitMyData DID-LLM: a verifiable identity for an AI agent, a bot, a person or an "
    "organization, with a credential signed by DebitMyData."
)
# What the app's service worker keeps, to open without a network.
WORKER_FILES = (
    *(f"/fonts/{name}" for name in FONTS),
    "/favicon.png",
    "/logo.png",
    "/logo-dark.png",
    "/app/icon-192.png",
    "/app/icon-512.png",
)


def _worker() -> str:
    """The service worker, with its file list and a cache named after their content: any
    change to one of them (a new icon, say) makes a new worker, and installed apps drop the
    old copies. It fetches them past the browser's own cache, which may hold older ones."""
    template = (_STATIC / "get-did-sw.js").read_text(encoding="utf-8")
    digest = hashlib.sha256(template.encode("utf-8"))
    for path in WORKER_FILES:
        digest.update((_STATIC / path.lstrip("/")).read_bytes())
    return template.replace("@CACHE@", f"dmd-did-app-{digest.hexdigest()[:12]}").replace(
        '["@FILES@"]', json.dumps(list(WORKER_FILES))
    )


_WORKER = _worker()


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


CSP = (
    f"default-src 'none'; script-src 'sha256-{_sha256(_SCRIPT)}' {brand.THEME_SCRIPT_SOURCE}; "
    f"style-src 'sha256-{_sha256(_STYLE)}'; font-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Cache-Control": f"no-store, {brand.NO_TRANSFORM}",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# What the DID is for: the participant types, in words people use.
PARTICIPANTS = (
    ("ai_agent", "An AI agent"),
    ("autonomous_agent", "An autonomous agent"),
    ("bot", "A bot"),
    ("agenticbot", "An agentic bot"),
    ("claw", "A claw"),
    ("digital_twin", "A digital twin"),
    ("human", "A person (myself)"),
    ("organization", "An organization"),
    ("approved_digital_entity", "Another approved digital entity"),
)


# What DMD is and how to get some: opened from the "?" under the DMD payment option.
_GET_DMD = """<div id=get-dmd class=explain hidden>
<h3>About DMD</h3>
<p>DMD is the native coin of the DebitMyData Proof-of-Work chain. Here it is simply one way to
pay for your DID-LLM: a payment method, not an investment.</p>
<h4>How to get DMD</h4>
<ol>
<li><strong>Mine it.</strong> Point SHA-256 mining hardware, your own or rented by the hour, at
the official pool, <a href="https://pool.debitmydata.io/" target=_blank
rel=noopener>pool.debitmydata.io</a>, which pays the DMD into your wallet. How much you mine
depends on your hashrate and the network's. Guides:
<a href="https://debitmydata.io/#asics" target=_blank rel=noopener>mining</a> and
<a href="https://debitmydata.io/#rent" target=_blank rel=noopener>renting hashrate</a>.</li>
<li><strong>Or pay with USDC</strong>, the US-dollar stablecoin, once it opens on this page.</li>
</ol>
<p>To hold and send DMD you need a DMD wallet:
<a href="https://debitmydata.io/#wallet" target=_blank rel=noopener>get one at
debitmydata.io</a>. DMD is not listed on any exchange yet.</p>
</div>"""


def _icon(paths: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
        f'focusable="false">{paths}</svg>'
    )


ICONS = {
    "tag": _icon('<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8" r="1.5"/>'),
    "steps": _icon(
        '<path d="M10 6h10M10 12h10M10 18h10"/><circle cx="5" cy="6" r="1.5"/>'
        '<circle cx="5" cy="12" r="1.5"/><circle cx="5" cy="18" r="1.5"/>'
    ),
    "clock": _icon('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    "no-account": _icon(
        '<circle cx="10" cy="8" r="4"/><path d="M3 21c1.2-3.8 3.8-6 7-6 1.4 0 2.7.4 3.8 1.2"/>'
        '<path d="M16 15l5 5M21 15l-5 5"/>'
    ),
    "globe": _icon(
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/>'
    ),
    "key": _icon('<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l3 3M14 9l2 2"/>'),
    "badge": _icon(
        '<rect x="3" y="4" width="18" height="12" rx="2"/><circle cx="12" cy="10" r="2.5"/>'
        '<path d="M9.5 20l2.5-1.5 2.5 1.5v-4"/>'
    ),
    "shield": _icon(
        '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>'
    ),
    "bot": _icon(
        '<rect x="4" y="8" width="16" height="11" rx="3"/><path d="M12 5v3"/>'
        '<circle cx="12" cy="4" r="1"/><path d="M9 13h.01M15 13h.01M9.5 16h5"/>'
    ),
    "person": _icon('<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>'),
    "building": _icon(
        '<rect x="5" y="3" width="14" height="18" rx="1"/>'
        '<path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2M11 21v-3h2v3"/>'
    ),
}


def _cards(items: tuple[tuple[str, str, str], ...], cls: str = "features") -> str:
    cards = "".join(
        f"<li class=feature><span class=icon>{ICONS[icon]}</span><h3>{title}</h3><p>{text}</p></li>"
        for icon, title, text in items
    )
    return f'<ul class="{cls}">{cards}</ul>'


STATS = (
    ("tag", "$25", "paid once"),
    ("steps", "4", "simple steps"),
    ("clock", "~15 min", "start to finish"),
    ("no-account", "0", "accounts or passwords"),
)
WHAT_IS_A_DID = (
    (
        "globe",
        "Its own address",
        "Your DID-LLM is a web address with a public document that tells anyone how to check "
        "that it is really you.",
    ),
    (
        "key",
        "You hold the key",
        "Only the owner of the wallet or key can prove they control the DID-LLM. DebitMyData "
        "never sees a private key.",
    ),
    (
        "badge",
        "A signed credential",
        "It comes with a verifiable credential signed by DebitMyData, which apps and agents "
        "can check on their own.",
    ),
    (
        "shield",
        "Trust without passwords",
        "Agents and apps recognise each other by their DID-LLMs, with no account or password "
        "to steal.",
    ),
)
WHO_IT_IS_FOR = (
    (
        "bot",
        "AI agents and bots",
        "Register by themselves through the API or the MCP server, pay automatically and "
        "prove control with their own key.",
    ),
    (
        "person",
        "People",
        "Get a personal DID-LLM right here in four steps, with your own wallet or key.",
    ),
    (
        "building",
        "Businesses",
        "Give your company and each of its agents an identity that partners can verify.",
    ),
)
HOW_IT_WORKS = (
    ("Tell us about it", "A name, what it is for, and the wallet or key that will own it."),
    ("Pay $25", "Once, with the payment option you choose."),
    ("Prove it is yours", "Sign a one-time message with your wallet or key. Nothing is sent."),
    ("Get your DID-LLM", "Your DID-LLM document and signed credential, ready to use."),
)


def _landing() -> str:
    """What a DID-LLM is, before the steps: numbers, what it is, who it is for, how it works."""
    stats = "".join(
        f"<li class=stat><span class=icon>{ICONS[icon]}</span><b>{number}</b>"
        f"<span class=label>{label}</span></li>"
        for icon, number, label in STATS
    )
    steps = "".join(
        f"<li><span class=n>{n}</span><h3>{title}</h3><p>{text}</p></li>"
        for n, (title, text) in enumerate(HOW_IT_WORKS, start=1)
    )
    return f"""<ul class=stats aria-label="In numbers">{stats}</ul>
<section class=intro id=what aria-labelledby=what-title>
<h2 class=section-title id=what-title>What is a <span class=accent>DID-LLM</span>?</h2>
<p class=lead>A DID-LLM is DebitMyData's identity for AI agents, the LLMs behind them, and
people. It is a Decentralized Identifier (DID), an open standard of the W3C, the organization
behind web standards. Think of it as an ID card for the internet, made for people and for
software alike.</p>
{_cards(WHAT_IS_A_DID)}
</section>
<section class=intro id=agents aria-labelledby=agents-title>
<h2 class=section-title id=agents-title>Why <span class=accent>AI agents</span> need one</h2>
<p class=lead>AI agents are programs that act on their own, often built on large language
models (LLMs), the technology behind AI assistants. They search, buy and talk to other agents
for the people and companies they work for. A DID-LLM lets an agent prove which agent it is and who
stands behind it, so that others can trust it.</p>
{_cards(WHO_IT_IS_FOR, "features three")}
</section>
<section class=intro id=how aria-labelledby=how-title>
<h2 class=section-title id=how-title>How it <span class=accent>works</span></h2>
<ol class=how>{steps}</ol>
<p class=cta><a class="button go" href="#start">Start now</a></p>
</section>"""


_SHARE_ICON = (
    '<svg viewBox="0 0 24 24" width=22 height=22 aria-hidden=true focusable=false>'
    '<path d="M12 3v12M8 7l4-4 4 4M7 10H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7'
    'a2 2 0 0 0-2-2h-1" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round '
    "stroke-linejoin=round /></svg>"
)
_ADD_ICON = (
    '<svg viewBox="0 0 24 24" width=22 height=22 aria-hidden=true focusable=false>'
    "<rect x=4 y=4 width=16 height=16 rx=4 fill=none stroke=currentColor stroke-width=2 />"
    '<path d="M12 8v8M8 12h8" fill=none stroke=currentColor stroke-width=2 '
    "stroke-linecap=round /></svg>"
)
# Offers the app, centred on the screen: by itself on phones and tablets, and on any device
# from the footer's "Get the app" button. The script picks what it shows: the Install button
# (when the browser can install it), the steps by hand (iPhone, Android's menu, a computer's
# browser), or that the app is already installed.
INSTALL_DIALOG = (
    "<dialog id=install class=install aria-labelledby=install-title>"
    "<div class=in>"
    '<button type=button id=install-close class=install-close aria-label="Close">&times;'
    "</button>"
    f'<img src="/app/icon-512.png?v={ICON_VERSION}" alt="" width=112 height=112>'
    # The title takes the focus when it opens (screen readers start there; no button is
    # pre-selected).
    f"<h2 id=install-title tabindex=-1 autofocus>Get the {APP_NAME} app</h2>"
    "<p id=install-how></p>"
    "<ol id=install-iphone class=steps hidden>"
    f"<li><span>Tap <b>Share</b></span>{_SHARE_ICON}</li>"
    f"<li><span>Choose <b>Add to Home Screen</b></span>{_ADD_ICON}</li></ol>"
    "<ol id=install-android class=steps hidden>"
    "<li><span>Open the browser's menu <b>&#8942;</b></span></li>"
    "<li><span>Tap <b>Install app</b> or <b>Add to Home screen</b></span></li></ol>"
    # Where the browser can't tell whether the app is installed (iPhone, Firefox...).
    "<p id=install-note class=note hidden>Already added it? Open it from your home screen.</p>"
    "<div class=install-actions>"
    "<button type=button id=install-go class=install-go hidden>Install the app</button>"
    "<button type=button id=install-later class=install-later>Not now</button>"
    "</div></div></dialog>"
)


def _for_agents(mcp_url: str) -> str:
    """For an AI agent reading this page: where to register by itself (no script needed)."""
    return (
        "<p class=for-agents><strong>AI agent or bot?</strong> Register by yourself: read "
        f'<a href="{brand.LLMS_PATH}">{brand.LLMS_PATH}</a>, or connect to the MCP server, the '
        f"Agentic Superhighway, at <code>{mcp_url}</code>.</p>"
    )


OWNER_KEY_GUIDE = "https://github.com/DebitMyData-PoW/agent-gateway/blob/main/docs/owner-key.md"


def _new_key_help(staging: bool) -> str:
    """Under "My agent's public key": how to make a key, with the owner-key tool. On staging, a
    test key of its own, and the guide's staging part."""
    guide = OWNER_KEY_GUIDE + ("#staging-the-test-server" if staging else "")
    name = "staging-test" if staging else "my-agent"
    test_key = (
        " On this test server, make a test key of its own: keep your real key for real DID-LLMs."
        if staging
        else ""
    )
    return (
        "<p class=small>No key yet? Make one on your own computer with the "
        f'<a href="{guide}" target=_blank rel=noopener>owner-key tool</a>: '
        f"<code>python3 did-owner-key.py new ~/did-keys/{name}.key</code> prints the public key "
        "to paste here. The private key stays on your computer: back it up, it is the proof "
        f"that the DID-LLM is yours.{test_key}</p>"
    )


def _body(staging: bool, app: bool = False, mcp_url: str = MCP_PATH, card: bool = False) -> str:
    # Visible buttons, not a drop-down list: AI agents (and people) see every choice at once.
    participants = "".join(
        f"<label class=pill><input type=radio name=kind value={value}"
        f"{' checked' if index == 0 else ''}>{label}</label>"
        for index, (value, label) in enumerate(PARTICIPANTS)
    )
    test_card = (
        ", or by card with Stripe's test card <strong>4242 4242 4242 4242</strong> (any future "
        "date, any CVC: no real card is charged)"
        if card
        else ""
    )
    staging_note = (
        "<p class=staging><strong>Staging server.</strong> Test networks and test DID-LLMs: pay "
        f"with the <strong>Pay with test coins</strong> button{test_card}, and sign with your "
        "own wallet (signing sends nothing).</p>"
        if staging
        else ""
    )
    # How people pay at step 2: in DMD, and by card once the card option is on.
    how_to_pay = "by card, or in DMD with any DMD wallet" if card else "in DMD, with any DMD wallet"
    where_you_pay = (
        "You sign in your own wallet, and pay in it or on Stripe's card payment page."
        if card
        else "You pay and sign in your own wallet."
    )
    # The app only: a window that offers to install it (get-did.js decides when).
    install = INSTALL_DIALOG if app else ""
    return f"""
<div id=countdown hidden aria-hidden=true><span class=label></span><span class=time></span></div>
{install}
{brand.header("get-did", staging, APP_ROOT if app else PAGE_PATH)}
<main>
<section class=hero>
<p class=eyebrow>Identity for the agentic web</p>
<h1>Get your <span class=accent>DebitMyData</span> DID-LLM</h1>
<p>A DID-LLM is a digital ID that its owner controls. It proves who an <strong>AI agent</strong>, a
bot, a <strong>person</strong> or an <strong>organization</strong> is, with a credential signed
by DebitMyData that anyone can check.</p>
<ul class=perks><li>Signed by DebitMyData</li><li>W3C standard</li><li>No account
needed</li></ul>
<p class=cta><a class="button go" href="#start">Get my DID-LLM</a>
<a class="button ghost" href="#what">What is a DID-LLM?</a></p>
<span class=price id=price>$25 USD, paid once</span>
{_for_agents(mcp_url)}
</section>
{staging_note}
<div id=resume class=resume hidden aria-live=polite></div>
{_landing()}
<section class=start id=start aria-labelledby=start-title>
<h2 class=section-title id=start-title>Get your <span class=accent>DID-LLM</span></h2>
<p class=safe><strong>Safe by design:</strong> this page never asks for a private key or a
recovery phrase, and never sends money. {where_you_pay}</p>
</section>

<section class="card step current" id=s1>
<div class=step-head><span class=num>1</span><h2>About your DID-LLM</h2></div>
<div class=body>
<form id=details novalidate>
<label class=field for=name>Name<small>Shown in the DID-LLM document, e.g. the agent's name.</small>
<input type=text id=name maxlength=100 autocomplete=off
placeholder="e.g. My research agent"></label>
<fieldset id=kind><legend>What is it for?</legend><div class=pills>{participants}</div></fieldset>
<fieldset>
<legend>Who owns it</legend>
<p class=small>Every DID-LLM needs an owner: a wallet or an agent's key. The owner proves it once
by signing one message (free: signing sends nothing).</p>
<label class=choice><input type=radio name=controller value=wallet checked>
<strong>My wallet</strong><span class=tag>Recommended</span>
<small>Any Ethereum-style wallet, whose address starts with 0x: MetaMask, Coinbase Wallet,
Rabby, Trust Wallet, a hardware wallet… It needs no balance (no USDC, no ETH): it only signs,
nothing is paid with it. No browser extension needed: you can type the address and sign in
your wallet app. You pay at step 2, {how_to_pay}.</small></label>
<div id=wallet-box class=sub>
<div class=wallets-holder></div>
<label class=field for=wallet-address><span id=wallet-address-label>Or type the wallet's
address (0x…)</span>
<input type=text id=wallet-address autocomplete=off spellcheck=false placeholder="0x…"></label>
<p class=hint id=wallet-hint hidden aria-live=polite></p>
</div>
<label class=choice><input type=radio name=controller value=key>
<strong>My agent's public key</strong>
<small>An Ed25519 key, for AI agents, or for people without a wallet app. You will sign one
message with its private key (the owner-key tool does it for you).</small></label>
<div id=key-box class=sub hidden>
<label class=field for=public-key>Public key
<input type=text id=public-key autocomplete=off spellcheck=false
placeholder="ed25519:z6Mk…"></label>
{_new_key_help(staging)}
</div>
</fieldset>
<fieldset>
<legend>How to pay</legend>
<div id=options><p class=small>Loading the payment options…</p>
<noscript><p class=small>Paying on this page needs JavaScript. AI agents:
<a href="/llms.txt">/llms.txt</a> explains how to register and pay through the API.</p></noscript>
</div>
{_GET_DMD}
</fieldset>
<label class=check><input type=checkbox id=terms><span>I accept the
<a href="/policies/terms" target=_blank>terms</a> and the
<a href="/policies/payment-methods" target=_blank>payment policy</a>, and I understand that a
DID-LLM registration is <a href="/policies/refunds" target=_blank>not refundable</a>.</span></label>
<p class=error id=details-error hidden role=alert></p>
<button type=submit id=get-quote>Get my quote</button>
</form>
</div>
</section>
<section class="card step waiting" id=s2>
<div class=step-head><span class=num>2</span><h2>Pay</h2></div>
<p class="small locked">Opens after <strong>Get my quote</strong> at step 1: you then pay
{how_to_pay}.</p><div class=body></div>
</section>
<section class="card step waiting" id=s3>
<div class=step-head><span class=num>3</span><h2>Prove it's yours</h2></div>
<p class="small locked">Opens once the payment is confirmed.</p><div class=body></div>
</section>
<section class="card step waiting" id=s4>
<div class=step-head><span class=num>4</span><h2>Your DID-LLM</h2></div>
<p class="small locked">Opens once the owner has signed.</p><div class=body></div>
</section>
<p id=announcer class=sr-only aria-live=polite></p>
</main>
{brand.footer(staging, APP_ROOT if app else PAGE_PATH)}
""".strip()


def page_html(staging: bool, app: bool = False, mcp_url: str = MCP_PATH, card: bool = False) -> str:
    config = json.dumps({"staging": staging, "app": app}).replace("<", "\\u003c")
    installable = (
        f'<link rel=manifest href="{MANIFEST_PATH}">'
        f'<meta name=theme-color content="{THEME_COLOR}">'
        "<meta name=mobile-web-app-capable content=yes>"
        "<meta name=apple-mobile-web-app-capable content=yes>"
        f'<meta name=apple-mobile-web-app-title content="{APP_NAME}">'
        "<meta name=apple-mobile-web-app-status-bar-style content=black>"
        f'<link rel=apple-touch-icon href="/app/apple-touch-icon.png?v={ICON_VERSION}">'
        if app
        else ""
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Get your DID-LLM · DebitMyData</title>"
        f'<meta name=description content="{DESCRIPTION}">'
        '<link rel=icon href="/favicon.png">'
        f"{installable}"
        f"{brand.THEME_SCRIPT}<style>{_STYLE}</style></head>"
        f"<body>{_body(staging, app, mcp_url, card)}"
        f'<script type="application/json" id="page-config">{config}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


def page_url(settings: Settings) -> str:
    """The Get a DID-LLM page's public address: the app's root, or /get-did on the gateway."""
    if settings.app_host:
        return f"https://{settings.app_host}/"
    return f"{settings.public_base_url.rstrip('/')}{PAGE_PATH}"


def mcp_url(settings: Settings) -> str:
    """The MCP server's address, as agents reach it (the same as /v1/discovery's mcp_url)."""
    return settings.mcp_url or f"{settings.public_base_url.rstrip('/')}{MCP_PATH}"


def _render(request: Request, app: bool) -> HTMLResponse:
    settings = request.app.state.settings
    staging = bool(settings.staging_test_tools)
    rails = request.app.state.registry.enabled_rails(DID_REGISTRATION)
    card = any(rail.protocol == STRIPE_CHECKOUT for _, rail in rails)
    return HTMLResponse(page_html(staging, app, mcp_url(settings), card), headers=HEADERS)


def _page(request: Request) -> Response:
    if is_app(request):  # one address for the app: its root
        return RedirectResponse(APP_ROOT, status_code=307)
    if moved := to_app(request, APP_ROOT):  # at the API's address: the app (hosts.py)
        return moved
    return _render(request, app=False)


def _root(request: Request) -> Response:
    """The site root: the app on the app's host; elsewhere the guided test (staging) or this
    page (production)."""
    if is_app(request):
        return _render(request, app=True)
    if request.app.state.settings.staging_test_tools:
        return RedirectResponse(brand.GUIDED_PATH, status_code=307)
    if moved := to_app(request, APP_ROOT):  # at the API's address: the app (hosts.py)
        return moved
    return RedirectResponse(PAGE_PATH, status_code=307)


router.add_api_route(PAGE_PATH, _page, methods=["GET"])
router.add_api_route(APP_ROOT, _root, methods=["GET"])


def _app_only(request: Request) -> None:
    # Nowhere else: an installed app is tied to one address.
    if not is_app(request):
        raise HTTPException(status_code=404)


def manifest_json(app_host: str) -> dict[str, object]:
    icons = [
        {
            "src": f"/app/icon-192.png?v={ICON_VERSION}",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": f"/app/icon-512.png?v={ICON_VERSION}",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": f"/app/icon-maskable-512.png?v={ICON_VERSION}",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ]
    return {
        "id": APP_ROOT,
        "name": APP_NAME,
        "short_name": APP_SHORT_NAME,
        "description": DESCRIPTION,
        "lang": "en",
        "start_url": APP_ROOT,
        "scope": APP_ROOT,
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": THEME_COLOR,
        "categories": ["business", "utilities"],
        "icons": icons,
        # The app itself, so that the page can ask the browser whether it is installed
        # (navigator.getInstalledRelatedApps: Chrome on Android, and on computers from
        # version 140). The page goes on offering it until then.
        "related_applications": [
            {"platform": "webapp", "url": MANIFEST_PATH, "id": f"https://{app_host}{APP_ROOT}"}
        ],
        "prefer_related_applications": False,
    }


@router.get(MANIFEST_PATH)
def manifest(request: Request) -> Response:
    _app_only(request)
    return Response(
        json.dumps(manifest_json(request.app.state.settings.app_host)),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@router.get(WORKER_PATH)
def service_worker(request: Request) -> Response:
    _app_only(request)
    return Response(
        _WORKER,
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-cache",  # a new version reaches installed apps at once
            "Content-Security-Policy": "default-src 'none'; connect-src 'self'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/app/{name}")
def app_icon(name: str, request: Request) -> Response:
    _app_only(request)
    if name not in APP_ICONS:
        raise HTTPException(status_code=404)
    return Response(
        (_STATIC / "app" / name).read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/fonts/{name}")
def font(name: str) -> Response:
    if name not in FONTS:
        raise HTTPException(status_code=404)
    return Response(
        (_STATIC / "fonts" / name).read_bytes(),
        media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"},
    )


def _advanced(request: Request) -> Response:
    # The API docs are for developers: at the app's address they lead to the API's (hosts.py).
    return to_api(request) or docs_pages.swagger_ui(request)


production_router.add_api_route(ADVANCED_PATH, _advanced, methods=["GET"])


def _to_page() -> RedirectResponse:
    return RedirectResponse(PAGE_PATH, status_code=307)


# The site root is _root above (it also serves the app on the app's host).
production_router.add_api_route(DOCS_PATH, _to_page, methods=["GET"])
