"""The credential issuer key (ID-15, DEC-06).

DebitMyData signs registration credentials with an Ed25519 key. Its public half is published
in the issuer's DID document (``/.well-known/did.json``), so anyone can verify a credential
without asking the gateway.

Development and staging read the private key from a PEM file that only its owner may read
(``DMD_GW_ISSUER_KEY_PATH``). Production must use a separate signer so the key never sits on
a public API host; until that exists, the settings refuse a key file in production.

Create a key with:

    dmd-gateway-issuer-key secrets/issuer-key.pem
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dmd_gateway.identity.crypto import ED25519_PUB_MULTICODEC, multibase_encode


class IssuerKeyError(RuntimeError):
    pass


@dataclass(frozen=True)
class IssuerKey:
    private_key: Ed25519PrivateKey
    did: str

    @property
    def public_key_multibase(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return multibase_encode(ED25519_PUB_MULTICODEC + raw)

    @property
    def verification_method(self) -> str:
        return f"{self.did}#{self.public_key_multibase}"


def load_issuer_key(path: Path, issuer_did: str) -> IssuerKey:
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
    return IssuerKey(private_key=key, did=issuer_did)


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
