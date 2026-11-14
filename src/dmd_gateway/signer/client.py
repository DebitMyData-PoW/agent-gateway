"""The API's side of the signer: an ``Issuer`` that asks ``dmd-gateway-signer`` for signatures,
and a token signer that asks it for OAuth access tokens.

The API knows the public keys from its settings (``DMD_GW_ISSUER_PUBLIC_KEY``,
``DMD_GW_OAUTH_PUBLIC_KEY_PATH``) and checks everything the signer returns against them
before using it, so a wrong or swapped signer key is caught at once instead of producing
credentials or tokens nobody can verify.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import verify_proof, xml_datetime
from dmd_gateway.oauth.keys import public_jwk
from dmd_gateway.oauth.tokens import IssuedToken, TokenRejected, verify_token
from dmd_gateway.signer.wire import WireError, encode, read_message

log = logging.getLogger("dmd_gateway.signer")
TIMEOUT_SECONDS = 10


class SignerUnavailable(RuntimeError):
    """The signer could not be reached or refused; nothing was signed."""


class SignerRefused(SignerUnavailable):
    """The signer was reached and refused after its own checks; nothing was signed."""


def call_signer(socket_path: Path, request: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
            sock.sendall(encode(request))
            response = read_message(sock)
    except (OSError, WireError) as exc:
        raise SignerUnavailable(f"credential signer unavailable: {exc}") from exc
    if response.get("ok") is not True:
        error = str(response.get("error", "no answer"))
        if error.startswith("refused:"):
            raise SignerRefused(f"credential signer: {error}")
        raise SignerUnavailable(f"credential signer: {error}")
    return response


@dataclass(frozen=True)
class RemoteIssuer:
    socket_path: Path
    did: str
    public_key_multibase: str
    timeout: float = TIMEOUT_SECONDS

    @property
    def verification_method(self) -> str:
        return f"{self.did}#{self.public_key_multibase}"

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        return call_signer(self.socket_path, request, self.timeout)

    def describe(self) -> tuple[str, str]:
        """The signer's DID and public key (the readiness check compares them)."""
        response = self._call({"op": "describe"})
        return str(response.get("did")), str(response.get("public_key_multibase"))

    def ready(self) -> bool:
        try:
            return self.describe() == (self.did, self.public_key_multibase)
        except SignerUnavailable:
            return False

    def sign(self, document: Mapping[str, Any], *, created: datetime) -> dict[str, Any]:
        """Have the signer sign ``document``. If it cannot, nothing is issued: the caller's
        transaction is rolled back and the agent is told to try again (HTTP 503)."""
        try:
            return self._sign(document, created)
        except SignerUnavailable as exc:
            log.error("credential not signed", extra={"event": {"error": str(exc)}})
            raise ApiError(
                503,
                "issuance_unavailable",
                "Credential issuance is temporarily unavailable; your payment is kept. Try "
                "again in a minute, without paying again.",
                headers={"Retry-After": "60"},
            ) from exc

    def _sign(self, document: Mapping[str, Any], created: datetime) -> dict[str, Any]:
        response = self._call(
            {"op": "sign", "document": dict(document), "created": xml_datetime(created)}
        )
        signed = response.get("document")
        if not isinstance(signed, dict):
            raise SignerUnavailable("credential signer: no signed document")
        unsigned = {key: value for key, value in signed.items() if key != "proof"}
        proof = signed.get("proof")
        if (
            unsigned != {key: value for key, value in document.items() if key != "proof"}
            or not isinstance(proof, dict)
            or proof.get("verificationMethod") != self.verification_method
            or not verify_proof(signed, self.public_key_multibase)
        ):
            raise SignerUnavailable(
                "credential signer returned a signature that does not match the issuer key"
            )
        return signed


@dataclass(frozen=True)
class RemoteTokenSigner:
    """Access tokens signed by the signer service (production): the API sends the agent's
    signed request; the signer checks it itself and signs. The API holds the public key only,
    and checks each token against it before handing it out."""

    socket_path: Path
    public_key: RSAPublicKey
    timeout: float = TIMEOUT_SECONDS

    @property
    def public_keys(self) -> tuple[RSAPublicKey, ...]:
        return (self.public_key,)

    def ready(self) -> bool:
        """The signer answers with this token key (the readiness check)."""
        try:
            described = call_signer(self.socket_path, {"op": "describe"}, self.timeout)
        except SignerUnavailable:
            return False
        token_key = described.get("token_key")
        return isinstance(token_key, dict) and (
            token_key.get("kid") == public_jwk(self.public_key)["kid"]
        )

    def issue(
        self,
        assertion: str,
        *,
        issuer: str,
        did: str,
        audience: str,
        scopes: tuple[str, ...],
        ttl_seconds: int,
        now: datetime,
    ) -> IssuedToken:
        """``SignerRefused`` if the signer's own checks fail, ``SignerUnavailable`` if it cannot
        be reached or returns a token that does not hold up."""
        response = call_signer(
            self.socket_path,
            {
                "op": "token",
                "assertion": assertion,
                "resource": audience,
                "scope": " ".join(scopes),
                "ttl_seconds": ttl_seconds,
            },
            self.timeout,
        )
        token = response.get("access_token")
        if not isinstance(token, str):
            raise SignerUnavailable("credential signer: no access token")
        try:
            claims = verify_token(
                token, self.public_key, issuer=issuer, audiences=[audience], now=now
            )
        except TokenRejected as exc:
            raise SignerUnavailable(
                f"credential signer returned a token that does not hold up: {exc.reason}"
            ) from exc
        if claims["sub"] != did or claims["scope"] != " ".join(scopes):
            raise SignerUnavailable("credential signer returned a token for something else")
        return IssuedToken(
            access_token=token,
            expires_in=int(claims["exp"]) - int(claims["iat"]),
            scope=claims["scope"],
            jti=str(claims["jti"]),
        )
