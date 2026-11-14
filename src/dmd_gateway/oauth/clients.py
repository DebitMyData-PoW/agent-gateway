"""The apps people sign in to (OAuth phase 2): OAuth clients identified by a Client ID Metadata
Document (CIMD, draft-ietf-oauth-client-id-metadata-document; the MCP specification's
recommended client registration).

An app's ``client_id`` is an https URL; the JSON document there names the app and its
redirect URIs, and this server fetches it when the app first asks, then keeps it a while. So
any MCP client (ChatGPT, Claude, an agent's own app) can ask people to sign in, with no
registration step, and a person always sees which site is asking.

Fetching an address an app chose is done carefully (``safe_fetch``): https only, on the
standard port, to a host whose every address is public (never this server, its network or a
cloud metadata address), with no redirects, a short timeout and a size limit; the certificate
must be valid for the host. An app that signs its token requests (``private_key_jwt``, as
ChatGPT may) has its keys fetched the same way.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

from dmd_gateway.oauth.assertion import CLOCK_SKEW_SECONDS, MAX_JTI_LENGTH, MAX_LIFETIME_SECONDS

MAX_DOCUMENT_BYTES = 64 * 1024
FETCH_TIMEOUT_SECONDS = 5
MIN_CACHE_SECONDS = 300
MAX_CACHE_SECONDS = 24 * 3600
MAX_CACHED = 256
MAX_NAME_LENGTH = 80
MAX_REDIRECT_URIS = 20
ASSERTION_ALGORITHMS = ("RS256", "PS256", "ES256", "EdDSA")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

Fetch = Callable[[str], tuple[bytes, Mapping[str, str]]]


class ClientRejected(Exception):
    """The app cannot be used: ``reason`` says why (shown to the person or the app)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Client:
    client_id: str
    name: str
    host: str  # the client_id's host: who is really asking, whatever the name says
    redirect_uris: tuple[str, ...]
    auth_methods: frozenset[str]  # how it authenticates at the token endpoint
    jwks: dict[str, Any] | None
    jwks_uri: str | None


def check_client_id(client_id: str) -> str:
    """The client_id's host, if it is a CIMD URL this server accepts, else ``ClientRejected``."""
    if len(client_id) > 512:
        raise ClientRejected("client_id is too long")
    parts = urlsplit(client_id)
    if parts.scheme != "https" or not parts.hostname:
        raise ClientRejected("client_id must be an https URL (a Client ID Metadata Document)")
    if parts.username or parts.password or parts.fragment or parts.query:
        raise ClientRejected("client_id must not have credentials, a query or a fragment")
    if parts.port not in (None, 443) or parts.path in ("", "/"):
        raise ClientRejected("client_id must be on port 443 and have a path")
    if parts.hostname in ("localhost",) or parts.hostname.endswith(".localhost"):
        raise ClientRejected("client_id must name a public site")
    return parts.hostname.lower()


def _public_addresses(host: str) -> None:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except (OSError, UnicodeError) as exc:
        raise ClientRejected(f"{host} could not be found") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise ClientRejected(f"{host} is not a public site")


def safe_fetch(url: str) -> tuple[bytes, Mapping[str, str]]:
    """GET a JSON document an app named, with the precautions in the module's docstring."""
    host = check_client_id(url)
    _public_addresses(host)
    try:
        with (
            httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client,
            client.stream(
                "GET", url, headers={"Accept": "application/json", "User-Agent": "DebitMyData"}
            ) as response,
        ):
            if response.status_code != 200:
                raise ClientRejected(f"{url} answered HTTP {response.status_code}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_DOCUMENT_BYTES:
                    raise ClientRejected(f"{url} is larger than {MAX_DOCUMENT_BYTES} bytes")
            return bytes(body), dict(response.headers)
    except httpx.HTTPError as exc:
        raise ClientRejected(f"{url} could not be fetched") from exc


def _max_age(headers: Mapping[str, str]) -> float:
    control = next((v for k, v in headers.items() if k.lower() == "cache-control"), "")
    for part in control.split(","):
        name, _, value = part.strip().partition("=")
        if name.lower() == "max-age" and value.strip().isdigit():
            return float(min(MAX_CACHE_SECONDS, max(MIN_CACHE_SECONDS, int(value))))
    return float(MIN_CACHE_SECONDS)


def _redirect_uri_ok(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.fragment or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    # Apps on the person's own computer (MCP desktop clients) use a loopback address.
    return parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS


def redirect_allowed(requested: str, registered: tuple[str, ...]) -> bool:
    """The app's return address must be one it lists, exactly; for a loopback address any port
    (RFC 8252 section 7.3: a native app picks its port when it runs, as Claude Code does)."""
    if requested in registered:
        return True
    asked = urlsplit(requested)
    if asked.scheme != "http" or asked.hostname not in LOOPBACK_HOSTS or asked.fragment:
        return False
    return any(
        (listed := urlsplit(uri)).scheme == "http"
        and listed.hostname == asked.hostname
        and listed.path == asked.path
        and listed.query == asked.query
        for uri in registered
    )


def parse_document(client_id: str, data: bytes) -> Client:
    host = check_client_id(client_id)
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientRejected("the app's metadata document is not JSON") from exc
    if not isinstance(document, dict):
        raise ClientRejected("the app's metadata document is not a JSON object")
    if document.get("client_id") != client_id:
        raise ClientRejected("the app's metadata document names another client_id")
    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip():
        raise ClientRejected("the app's metadata document has no client_name")
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
        raise ClientRejected("the app's metadata document has no usable redirect_uris")
    kept = tuple(uri for uri in uris if isinstance(uri, str) and _redirect_uri_ok(uri))
    if not kept:
        raise ClientRejected("none of the app's redirect_uris is https (or a loopback address)")
    methods = document.get("token_endpoint_auth_methods_supported")
    method = document.get("token_endpoint_auth_method")
    found = set(methods) if isinstance(methods, list) else set()
    if isinstance(method, str):
        found.add(method)
    auth = frozenset(found & {"none", "private_key_jwt"}) or frozenset({"none"})
    jwks = document.get("jwks") if isinstance(document.get("jwks"), dict) else None
    jwks_uri = document.get("jwks_uri") if isinstance(document.get("jwks_uri"), str) else None
    if auth == frozenset({"private_key_jwt"}) and jwks is None and jwks_uri is None:
        raise ClientRejected("the app signs its requests but publishes no keys (jwks_uri)")
    clean = " ".join(name.split())[:MAX_NAME_LENGTH]
    return Client(client_id, clean, host, kept, auth, jwks, jwks_uri)


class ClientDirectory:
    """The apps' metadata documents (and keys), fetched when needed and kept a while."""

    def __init__(self, fetch: Fetch = safe_fetch, clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: dict[str, tuple[float, Client]] = {}
        self._keys: dict[str, tuple[float, dict[str, Any]]] = {}

    def _remember(self, cache: dict[str, Any], key: str, value: Any, max_age: float) -> None:
        with self._lock:
            if len(cache) >= MAX_CACHED:
                cache.pop(min(cache, key=lambda k: cache[k][0]))
            cache[key] = (self._clock() + max_age, value)

    def _cached(self, cache: dict[str, Any], key: str) -> Any:
        with self._lock:
            found = cache.get(key)
        return found[1] if found and found[0] > self._clock() else None

    def get(self, client_id: str) -> Client:
        check_client_id(client_id)
        client = self._cached(self._clients, client_id)
        if client is None:
            data, headers = self._fetch(client_id)
            client = parse_document(client_id, data)
            self._remember(self._clients, client_id, client, _max_age(headers))
        return client

    def keys(self, client: Client) -> dict[str, Any]:
        if client.jwks is not None:
            return client.jwks
        assert client.jwks_uri is not None
        keys = self._cached(self._keys, client.jwks_uri)
        if keys is None:
            data, headers = self._fetch(client.jwks_uri)
            try:
                keys = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ClientRejected("the app's keys (jwks_uri) are not JSON") from exc
            if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list):
                raise ClientRejected("the app's keys (jwks_uri) are not a JWK set")
            self._remember(self._keys, client.jwks_uri, keys, _max_age(headers))
        return keys


@dataclass(frozen=True)
class ClientAssertion:
    jti: str
    expires_at: datetime


def verify_client_assertion(
    token: str, client: Client, keys: dict[str, Any], *, audience: list[str], now: datetime
) -> ClientAssertion:
    """An app's signed token request (``private_key_jwt``, RFC 7523), checked with its keys."""
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in ASSERTION_ALGORITHMS:
            raise ClientRejected("the client_assertion's algorithm is not accepted")
        candidates = [
            entry
            for entry in jwt.PyJWKSet.from_dict(keys).keys
            if header.get("kid") in (None, entry.key_id)
        ]
        if not candidates:
            raise ClientRejected("the client_assertion is signed with a key the app does not list")
        claims: dict[str, Any] | None = None
        for candidate in candidates:
            try:
                claims = jwt.decode(
                    token,
                    candidate.key,
                    algorithms=[algorithm],
                    audience=audience,
                    issuer=client.client_id,
                    options={"require": ["iss", "sub", "aud", "exp", "jti"], "verify_exp": False},
                )
                break
            except jwt.InvalidSignatureError:
                continue
        if claims is None:
            raise ClientRejected("the client_assertion's signature does not match the app's keys")
    except jwt.PyJWTError as exc:
        raise ClientRejected(f"the client_assertion is not valid: {exc}") from exc
    moment = int(now.timestamp())
    expires = claims["exp"]
    if claims["sub"] != client.client_id:
        raise ClientRejected("the client_assertion's sub must be the client_id")
    if not isinstance(expires, int) or expires < moment - CLOCK_SKEW_SECONDS:
        raise ClientRejected("the client_assertion has expired")
    if expires > moment + MAX_LIFETIME_SECONDS + CLOCK_SKEW_SECONDS:
        raise ClientRejected("the client_assertion must expire within 5 minutes")
    jti = claims["jti"]
    if not isinstance(jti, str) or not 0 < len(jti) <= MAX_JTI_LENGTH:
        raise ClientRejected("the client_assertion's jti must be a short string")
    return ClientAssertion(jti=jti, expires_at=datetime.fromtimestamp(expires, now.tzinfo))
