"""The API's side of the signer: an ``Issuer`` that asks ``dmd-gateway-signer`` for signatures.

The API knows the issuer's public key from its settings (``DMD_GW_ISSUER_PUBLIC_KEY``) and
checks every signature the signer returns against it before using it, so a wrong or swapped
signer key is caught at once instead of producing credentials nobody can verify.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dmd_gateway.errors import ApiError
from dmd_gateway.identity.crypto import verify_proof, xml_datetime
from dmd_gateway.signer.wire import WireError, encode, read_message

log = logging.getLogger("dmd_gateway.signer")
TIMEOUT_SECONDS = 10


class SignerUnavailable(RuntimeError):
    """The signer could not be reached or refused; nothing was signed."""


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
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.socket_path))
                sock.sendall(encode(request))
                response = read_message(sock)
        except (OSError, WireError) as exc:
            raise SignerUnavailable(f"credential signer unavailable: {exc}") from exc
        if response.get("ok") is not True:
            raise SignerUnavailable(f"credential signer: {response.get('error', 'no answer')}")
        return response

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
