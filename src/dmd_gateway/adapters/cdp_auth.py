"""Authentication for Coinbase Developer Platform (CDP) APIs, used by the CDP x402 facilitator.

Each request carries a short-lived JWT (2 minutes) signed with a CDP "Secret API key":
``Authorization: Bearer <jwt>``. The JWT names one request only, in its ``uris`` claim
(``POST api.cdp.coinbase.com/platform/v2/x402/settle``), so a token seen in transit cannot be
reused for another call. Two key types exist: Ed25519 (recommended; the secret is base64 of
64 bytes, seed then public key) and ECDSA P-256 (legacy; the secret is a PEM block).

The key secret comes from the environment and is never logged or returned.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

TOKEN_LIFETIME_SECONDS = 120


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_key(secret: str) -> tuple[str, Ed25519PrivateKey | ec.EllipticCurvePrivateKey]:
    """The JWT algorithm and the private key of a CDP key secret."""
    text = secret.strip().replace("\\n", "\n")
    if not text:
        raise ValueError("the CDP API key secret is empty")
    if text.startswith("-----BEGIN"):
        try:
            key = serialization.load_pem_private_key(text.encode(), password=None)
        except (ValueError, TypeError) as exc:
            raise ValueError("the CDP API key secret is not a valid PEM private key") from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("a PEM CDP API key secret must be an ECDSA P-256 key")
        return "ES256", key
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise ValueError("the CDP API key secret is neither base64 (Ed25519) nor PEM") from exc
    if len(raw) != 64:
        raise ValueError("an Ed25519 CDP API key secret must decode to 64 bytes")
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if public != raw[32:]:
        raise ValueError("the CDP API key secret is damaged (its public half does not match)")
    return "EdDSA", key


class CdpApiKeyAuth:
    """Makes the ``Authorization`` header for one CDP API request."""

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.key_id = key_id.strip()
        if not self.key_id:
            raise ValueError("the CDP API key ID is empty")
        self._algorithm, self._key = _load_key(key_secret)
        self._clock = clock

    def __repr__(self) -> str:  # never show the key
        return f"CdpApiKeyAuth(key_id={self.key_id!r}, algorithm={self._algorithm!r})"

    def token(self, method: str, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("CDP requests must use an https URL")
        now = int(self._clock())
        header = {
            "alg": self._algorithm,
            "kid": self.key_id,
            "typ": "JWT",
            "nonce": secrets.token_hex(8),
        }
        # The same claims as Coinbase's own SDK (cdp-sdk generate_jwt), which its facilitator
        # clients use: no audience, and the request in a one-item "uris" list.
        claims = {
            "sub": self.key_id,
            "iss": "cdp",
            "aud": None,
            "nbf": now,
            "exp": now + TOKEN_LIFETIME_SECONDS,
            "uris": [f"{method.upper()} {parts.netloc}{parts.path}"],
        }
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
        ).encode("ascii")
        if isinstance(self._key, Ed25519PrivateKey):
            signature = self._key.sign(signing_input)
        else:
            r, s = decode_dss_signature(self._key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
            signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{signing_input.decode('ascii')}.{_b64url(signature)}"

    def __call__(self, method: str, url: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(method, url)}"}
