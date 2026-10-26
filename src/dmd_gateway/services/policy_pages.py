"""Policy pages (DEC-09): terms, privacy, security, payment methods, refunds and the DID policy.

The gateway serves its own policies, so every quote can link to pages that exist and match
the server that made it. The texts are Markdown files in ``dmd_gateway/policies``, one per
page, each starting with a small header:

    ---
    title: Refund policy
    version: 2026-10-25        # the date of this version of the text
    status: draft              # draft until DebitMyData approves it, then approved
    summary: One sentence for the page list.
    ---

A text can use these placeholders, filled in for the server it runs on:

    {{ base_url }}         e.g. https://api.debitmydata.io
    {{ host }}             the host part of the DIDs, e.g. api.debitmydata.io
    {{ issuer_did }}       the DID that signs credentials
    {{ quote_minutes }}    how long a quote is valid
    {{ challenge_minutes }}  how long a controller challenge is valid
    {{ payment_methods }}  a table of the approved payment methods of this server
    {{ dmd_price_rule }}   how the DMD/USD reference price is set (with its minimum rate)

The terms page's version is the ``terms_version`` that quotes name (checked by the tests).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from typing import Any

import yaml
from markdown_it import MarkdownIt

from dmd_gateway.domain.routes import PaymentRouteRegistry
from dmd_gateway.identity.did import did_web_host, issuer_did

# Page name -> the key under which quotes and discovery link to it (policy_urls).
PAGES = {
    "terms": "terms",
    "privacy": "privacy",
    "security": "security",
    "payment-methods": "payment_policy",
    "refunds": "refund_policy",
    "did": "did_policy",
}
STATUSES = ("draft", "approved")
PATH = "/policies"

_TEXTS = files("dmd_gateway") / "policies"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_HEADER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Commonmark with tables; raw HTML in the texts is shown as text, never run.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")

_NETWORKS = {
    "dmd:mainnet": "DebitMyData PoW",
    "dmd:regtest": "DebitMyData private test chain (no value)",
    "eip155:8453": "Base",
    "eip155:84532": "Base Sepolia (test network, no value)",
    "solana:mainnet": "Solana",
}


class PolicyPageError(ValueError):
    """A policy text that cannot be served: bad header or unknown placeholder."""


@dataclass(frozen=True)
class PolicyPage:
    name: str
    title: str
    version: date
    status: str
    summary: str
    markdown: str  # with the placeholders filled in
    html: str  # the body only

    @property
    def path(self) -> str:
        return f"{PATH}/{self.name}"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()


def load_policy_pages(
    *,
    public_base_url: str,
    registry: PaymentRouteRegistry,
    quote_minutes: int,
    challenge_minutes: int,
) -> dict[str, PolicyPage]:
    base = public_base_url.rstrip("/")
    values = {
        "base_url": base,
        "host": did_web_host(base),
        "issuer_did": issuer_did(base),
        "quote_minutes": str(quote_minutes),
        "challenge_minutes": str(challenge_minutes),
        "payment_methods": payment_methods_table(registry),
        "dmd_price_rule": dmd_price_rule(registry),
    }
    return {name: _page(name, values) for name in PAGES}


def dmd_price_rule(registry: PaymentRouteRegistry) -> str:
    cost = "the cost of mining DMD: the electricity that the work of a block takes"
    parameters = registry.dmd_mining_cost
    if parameters is None or parameters.minimum_usd_per_dmd is None:
        return cost
    minimum = format(parameters.minimum_usd_per_dmd.normalize(), "f")
    return (
        f"the higher of DebitMyData's minimum rate of {minimum} USD per DMD and {cost}. "
        "While DMD is young and its mining power small, the minimum rate applies"
    )


def _page(name: str, values: Mapping[str, str]) -> PolicyPage:
    text = (_TEXTS / f"{name}.md").read_text(encoding="utf-8")
    header = _HEADER.match(text)
    if header is None:
        raise PolicyPageError(f"{name}.md must start with a --- header ---")
    meta: Any = yaml.safe_load(header.group(1))
    if not isinstance(meta, dict) or set(meta) != {"title", "version", "status", "summary"}:
        raise PolicyPageError(f"{name}.md: the header needs title, version, status, summary")
    if not isinstance(meta["version"], date) or meta["status"] not in STATUSES:
        raise PolicyPageError(f"{name}.md: version is a date; status is draft or approved")

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise PolicyPageError(f"{name}.md: unknown placeholder {{{{ {key} }}}}")
        return values[key]

    markdown = _PLACEHOLDER.sub(fill, text[header.end() :]).strip() + "\n"
    return PolicyPage(
        name=name,
        title=str(meta["title"]),
        version=meta["version"],
        status=str(meta["status"]),
        summary=str(meta["summary"]),
        markdown=markdown,
        html=_MARKDOWN.render(markdown),
    )


def payment_methods_table(registry: PaymentRouteRegistry) -> str:
    """The approved payment methods of this server, as a Markdown table."""
    rows = [
        "| Method | Asset | Network | Token contract | Confirmations |",
        "| --- | --- | --- | --- | --- |",
    ]
    for rail_id, rail in registry.rails.items():
        if not rail.approved:
            continue
        network = _NETWORKS.get(rail.network, rail.network)
        contract = f"`{rail.token_contract}`" if rail.token_contract else "native coin"
        required = rail.confirmation_policy.required_confirmations
        confirmations = str(required) if required is not None else "-"
        rows.append(
            f"| {rail.name} (`{rail_id}`) | {rail.asset.symbol} | {network} (`{rail.network}`) "
            f"| {contract} | {confirmations} |"
        )
    return "\n".join(rows)


def policy_links(urls: Mapping[str, str], public_base_url: str) -> dict[str, str]:
    """Policy URLs as quotes and discovery publish them: paths on this server (such as
    ``/policies/terms``) become full URLs."""
    base = public_base_url.rstrip("/")
    return {key: f"{base}{url}" if url.startswith("/") else url for key, url in urls.items()}
