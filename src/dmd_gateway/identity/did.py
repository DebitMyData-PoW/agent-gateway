"""DebitMyData identifiers and DID documents (DEC-05).

Identities use the standard ``did:web`` method, hosted by the gateway itself:

    did:web:<gateway host>:agents:<id>   ->  https://<gateway host>/agents/<id>/did.json
    did:web:<gateway host>               ->  https://<gateway host>/.well-known/did.json (issuer)

``did:web`` is an existing DID method, so these identifiers resolve with ordinary DID
tooling. A dedicated ``did:dmd`` method is not claimed until its specification is published.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from dmd_gateway.identity.crypto import ControllerKey

DID_CONTEXT = ["https://www.w3.org/ns/did/v1", "https://w3id.org/security/multikey/v1"]
AGENTS_SEGMENT = "agents"


def did_web_host(public_base_url: str) -> str:
    """``https://api.example.com`` -> ``api.example.com``; a port is encoded as ``%3A``."""
    parts = urlsplit(public_base_url)
    if not parts.hostname:
        raise ValueError("public_base_url has no host name")
    host = parts.hostname.lower()
    if parts.port:
        host = f"{host}%3A{parts.port}"
    return host


def issuer_did(public_base_url: str) -> str:
    return f"did:web:{did_web_host(public_base_url)}"


def agent_did(public_base_url: str, identity_id: str) -> str:
    return f"did:web:{did_web_host(public_base_url)}:{AGENTS_SEGMENT}:{did_suffix(identity_id)}"


def did_suffix(identity_id: str) -> str:
    """``dmd_identity_01J...`` -> ``01J...`` (the part used in the DID and its URL)."""
    return identity_id.rsplit("_", 1)[-1]


def did_document_url(public_base_url: str, identity_id: str) -> str:
    base = public_base_url.rstrip("/")
    return f"{base}/{AGENTS_SEGMENT}/{quote(did_suffix(identity_id))}/did.json"


def agent_did_document(did: str, controller: ControllerKey, *, record_url: str) -> dict[str, Any]:
    key_id = f"{did}#{controller.multibase}"
    return {
        "@context": DID_CONTEXT,
        "id": did,
        "verificationMethod": [
            {
                "id": key_id,
                "type": "Multikey",
                "controller": did,
                "publicKeyMultibase": controller.multibase,
            }
        ],
        "authentication": [key_id],
        "assertionMethod": [key_id],
        "service": [
            {
                "id": f"{did}#debitmydata",
                "type": "DebitMyDataIdentityRecord",
                "serviceEndpoint": record_url,
            }
        ],
    }


def issuer_did_document(did: str, public_key_multibase: str) -> dict[str, Any]:
    key_id = f"{did}#{public_key_multibase}"
    return {
        "@context": DID_CONTEXT,
        "id": did,
        "verificationMethod": [
            {
                "id": key_id,
                "type": "Multikey",
                "controller": did,
                "publicKeyMultibase": public_key_multibase,
            }
        ],
        "assertionMethod": [key_id],
    }
