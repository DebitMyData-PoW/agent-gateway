"""Agents sign in with their DID-LLM's wallet: the discovery runbook's "wallet-signature" path,
for crypto-native agents whose DID-LLM is controlled by a wallet (an EVM address) rather than
an Ed25519 key, with no browser.

    POST https://auth.debitmydata.io/challenge   did=...  (resource=..., scope=...)
         -> {"message": "DebitMyData agent sign-in\\n...", "expires_in": 300}
    the agent signs the message with its wallet (EIP-191, personal_sign)
    POST https://auth.debitmydata.io/token
         grant_type=urn:debitmydata:params:oauth:grant-type:wallet-signature
         message=<the message>  signature=0x...
         -> an access token for the MCP server, as with the agent's key (phase 1)

It is an OAuth extension grant (RFC 6749 section 4.5): the token is the same RS256 access
token, checked the same way by the MCP server. The message is the server's, with a random
nonce, for one DID-LLM, scope, MCP server and sign-in server, valid 5 minutes and used once;
nothing is kept until it is used. The wallet must be the DID-LLM's owner wallet now. In
production the signer service checks the message and the signature again itself before it
signs the token; it has no network, so a smart wallet (ERC-1271), which only its chain can
check, cannot sign in there yet: an ordinary wallet can.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from dmd_gateway.identity.crypto import xml_datetime
from dmd_gateway.oauth.grants import GrantRefused, message_time, scope_words
from dmd_gateway.oauth.tokens import CLOCK_SKEW_SECONDS, SCOPES

GRANT_TYPE = "urn:debitmydata:params:oauth:grant-type:wallet-signature"
CHALLENGE_PATH = "/challenge"
TITLE = "DebitMyData agent sign-in"
MESSAGE_SECONDS = 300
NONCE_PREFIX = "wallet:"  # used nonces share the signed requests' table, under this prefix
_FIELDS = ("DID-LLM", "Allows", "For", "Sign-in server", "Nonce", "Issued", "Expires")


@dataclass(frozen=True)
class WalletMessage:
    did: str
    allows: str
    resource: str
    issuer: str
    nonce: str
    issued: datetime
    expires: datetime


def challenge(*, did: str, scope: str, resource: str, issuer: str, now: datetime) -> str:
    """The message the agent signs with its wallet (lines joined by \\n, no trailing one)."""
    now = now.replace(microsecond=0)
    return "\n".join(
        [
            TITLE,
            f"DID-LLM: {did}",
            f"Allows: {scope_words(scope)}",
            f"For: {resource}",
            f"Sign-in server: {issuer}",
            f"Nonce: {secrets.token_urlsafe(18)}",
            f"Issued: {xml_datetime(now)}",
            f"Expires: {xml_datetime(now + timedelta(seconds=MESSAGE_SECONDS))}",
        ]
    )


def parse(text: str) -> WalletMessage:
    lines = text.replace("\r\n", "\n").split("\n")
    if len(lines) != len(_FIELDS) + 1 or lines[0] != TITLE:
        raise GrantRefused("not a DebitMyData agent sign-in message: ask /challenge for one")
    values: dict[str, str] = {}
    for name, line in zip(_FIELDS, lines[1:], strict=True):
        prefix = f"{name}: "
        if not line.startswith(prefix) or not line[len(prefix) :].strip():
            raise GrantRefused(f"the message's line {name!r} is missing")
        values[name] = line[len(prefix) :]
    return WalletMessage(
        did=values["DID-LLM"],
        allows=values["Allows"],
        resource=values["For"],
        issuer=values["Sign-in server"],
        nonce=values["Nonce"],
        issued=message_time(values["Issued"]),
        expires=message_time(values["Expires"]),
    )


def check(
    text: str,
    *,
    issuer: str,
    resources: Sequence[str],
    now: datetime,
    scope: str | None = None,
    resource: str | None = None,
) -> tuple[WalletMessage, str]:
    """The message and the scope it allows, if it is this sign-in server's, for one of its MCP
    addresses, still valid; ``scope`` and ``resource``, when the request names them, must be
    the message's. Otherwise ``GrantRefused``."""
    message = parse(text)
    skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
    if message.issuer != issuer:
        raise GrantRefused("the message is for another sign-in server")
    if message.resource not in resources or (resource and resource != message.resource):
        raise GrantRefused("the message is for another service")
    names = [name for name in SCOPES if scope_words(name) in message.allows.split("; ")]
    granted = " ".join(names)
    if not names or scope_words(granted) != message.allows:
        raise GrantRefused("the message allows something unknown")
    if scope and sorted(scope.split()) != sorted(names):
        raise GrantRefused("scope must be the one in the message")
    if not 0 < len(message.nonce) <= 64:
        raise GrantRefused("the message's nonce is not usable")
    if message.issued > now + skew:
        raise GrantRefused("the message was issued in the future")
    if message.expires < now - skew:
        raise GrantRefused("the message has expired: ask /challenge for a new one")
    if message.expires - message.issued > timedelta(seconds=MESSAGE_SECONDS) + skew:
        raise GrantRefused("a sign-in message lasts 5 minutes at most")
    return message, granted
