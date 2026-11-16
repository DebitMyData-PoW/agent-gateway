"""The credential issuer key (ID-15, DEC-06).

DebitMyData signs registration credentials with an Ed25519 key. Its public half is published
in the issuer's DID document (``/.well-known/did.json``), so anyone can verify a credential
without asking the gateway.

Development and staging read the private key from a PEM file that only its owner may read
(``DMD_GW_ISSUER_KEY_PATH``). Production keeps the key in a separate signer service
(``dmd_gateway.signer``): the API asks it for each signature over a local socket and never
holds the key; the settings refuse a key file in production.

Create a key with:

    dmd-gateway-issuer-key secrets/issuer-key.pem

Replacing the key (a rotation, docs/production.md) keeps the credentials and receipts signed
so far valid: the earlier public keys stay listed as **retired keys**
(``DMD_GW_ISSUER_RETIRED_KEYS``) in the issuer's DID document, where they verify what they
signed. Nothing new is signed with them.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dmd_gateway.identity.crypto import (
    ED25519_PUB_MULTICODEC,
    EncodingError,
    add_proof,
    multibase_decode,
    multibase_encode,
    verify_proof,
)

# An Ed25519 public key as a Multikey (base58btc, multicodec 0xed01): z6Mk...
MULTIKEY = re.compile(r"z6Mk[1-9A-HJ-NP-Za-km-z]{40,50}")


class IssuerKeyError(RuntimeError):
    pass


def check_public_key(value: str) -> str:
    """``value`` if it is an Ed25519 public key in Multikey form, or ValueError."""
    if not MULTIKEY.fullmatch(value):
        raise ValueError(f"{value!r} is not an Ed25519 public key (z6Mk...)")
    try:
        raw = multibase_decode(value)
    except EncodingError as exc:
        raise ValueError(f"{value!r} is not an Ed25519 public key (z6Mk...)") from exc
    if not raw.startswith(ED25519_PUB_MULTICODEC) or len(raw) != 34:
        raise ValueError(f"{value!r} is not an Ed25519 public key (z6Mk...)")
    return value


def parse_retired_keys(text: str | None) -> tuple[str, ...]:
    """The retired issuer keys, from ``z6Mk...,z6Mk...`` (commas, spaces or both)."""
    found: list[str] = []
    for value in re.split(r"[\s,]+", (text or "").strip().strip("'\"")):
        if value and check_public_key(value) not in found:
            found.append(value)
    return tuple(found)


@dataclass(frozen=True)
class IssuerKeys:
    """The public keys DebitMyData's credentials and receipts are signed with: the current
    one, and the retired ones that still verify what they signed (newest first)."""

    did: str
    current: str
    retired: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.current in self.retired:
            raise ValueError("the current issuer key cannot also be a retired one")

    def method(self, public_key: str) -> str:
        return f"{self.did}#{public_key}"

    @property
    def methods(self) -> dict[str, str]:
        """Verification method ID -> public key, the current key first."""
        return {self.method(key): key for key in (self.current, *self.retired)}

    def verifies(self, document: Mapping[str, Any]) -> bool:
        """Whether ``document`` carries a valid proof by one of these keys (any of them, so
        what a retired key signed before the rotation still counts)."""
        proof = document.get("proof")
        if not isinstance(proof, Mapping) or proof.get("proofPurpose") != "assertionMethod":
            return False
        key = self.methods.get(str(proof.get("verificationMethod")))
        return key is not None and verify_proof(document, key)


class Issuer(Protocol):
    """What signs credentials: a local key (development, staging) or the signer service."""

    @property
    def did(self) -> str: ...

    @property
    def public_key_multibase(self) -> str: ...

    @property
    def verification_method(self) -> str: ...

    @property
    def retired_keys(self) -> tuple[str, ...]:
        """Earlier public keys, still listed so what they signed keeps verifying."""
        ...

    def sign(self, document: Mapping[str, Any], *, created: datetime) -> dict[str, Any]:
        """``document`` with its ``eddsa-jcs-2022`` proof attached."""
        ...


@dataclass(frozen=True)
class IssuerKey:
    private_key: Ed25519PrivateKey
    did: str
    retired_keys: tuple[str, ...] = ()

    def sign(self, document: Mapping[str, Any], *, created: datetime) -> dict[str, Any]:
        return add_proof(
            document,
            self.private_key,
            verification_method=self.verification_method,
            created=created,
        )

    @property
    def public_key_multibase(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return multibase_encode(ED25519_PUB_MULTICODEC + raw)

    @property
    def verification_method(self) -> str:
        return f"{self.did}#{self.public_key_multibase}"


def issuer_keys(issuer: Issuer) -> IssuerKeys:
    """The issuer's current and retired public keys."""
    return IssuerKeys(issuer.did, issuer.public_key_multibase, tuple(issuer.retired_keys))


def load_issuer_key(path: Path, issuer_did: str, retired_keys: tuple[str, ...] = ()) -> IssuerKey:
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise IssuerKeyError(f"issuer key file not found: {path}") from exc
    if os.name == "posix" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise IssuerKeyError(
            f"issuer key file {path} must be readable by its owner only (chmod 600 {path})"
        )
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise IssuerKeyError(f"{path} is not an unencrypted PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise IssuerKeyError(f"{path} is not an Ed25519 key")
    issuer = IssuerKey(private_key=key, did=issuer_did, retired_keys=retired_keys)
    if issuer.public_key_multibase in retired_keys:
        raise IssuerKeyError(
            f"the key in {path} is listed as a retired issuer key: a retired key never signs "
            "again (docs/production.md, replacing the issuer key)"
        )
    return issuer


def write_new_key(path: Path) -> str:
    """Create a new Ed25519 key file (mode 600). Returns the public key (Multikey)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return multibase_encode(ED25519_PUB_MULTICODEC + raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dmd-gateway-issuer-key",
        description="Create the Ed25519 key the gateway signs credentials with.",
    )
    parser.add_argument("path", type=Path, help="where to write the key, e.g. secrets/issuer.pem")
    args = parser.parse_args(argv)
    try:
        public = write_new_key(args.path)
    except FileExistsError:
        print(f"{args.path} already exists; refusing to overwrite it.", file=sys.stderr)
        return 1
    print(f"Created {args.path} (readable by its owner only).")
    print(f"Public key: {public}")
    print(f"Set DMD_GW_ISSUER_KEY_PATH={args.path} and keep the file out of version control.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
