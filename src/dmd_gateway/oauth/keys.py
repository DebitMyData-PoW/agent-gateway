"""The key that signs access tokens: RSA, used with RS256 (the discovery runbook's choice and
what every OAuth client library verifies). Development and staging keep it in a file
(``oauth_key_path``, mode 600); production keeps it in the signer service.

Its public half is published as a JWK set at auth.debitmydata.io/.well-known/jwks.json, with a
``kid`` that is the key's RFC 7638 thumbprint, so a new key can be published beside the old one
when it is changed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
    RSAPublicNumbers,
    generate_private_key,
)

KEY_SIZE = 3072


class OAuthKeyError(RuntimeError):
    pass


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_jwk(key: RSAPublicKey) -> dict[str, str]:
    """The public key as a JWK, with its RFC 7638 thumbprint as ``kid``."""
    numbers = key.public_numbers()
    members = {"e": _b64url_uint(numbers.e), "kty": "RSA", "n": _b64url_uint(numbers.n)}
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode("ascii")
    kid = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()
    return {**members, "kid": kid, "use": "sig", "alg": "RS256"}


def jwk_set(*keys: RSAPublicKey) -> dict[str, Any]:
    return {"keys": [public_jwk(key) for key in keys]}


def load_private_key(path: Path) -> RSAPrivateKey:
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise OAuthKeyError(f"token-signing key file not found: {path}") from exc
    if os.name == "posix" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise OAuthKeyError(f"{path} must be readable by its owner only (chmod 600 {path})")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise OAuthKeyError(f"{path} is not an unencrypted PEM private key") from exc
    if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
        raise OAuthKeyError(f"{path} is not an RSA key of 2048 bits or more")
    return key


def load_public_jwk(text: str) -> RSAPublicKey:
    """The public key from its JWK (JSON), as the production API gets it from its settings."""
    try:
        jwk = json.loads(text)
        if jwk.get("kty") != "RSA":
            raise ValueError("not an RSA key")
        n = int.from_bytes(base64.urlsafe_b64decode(jwk["n"] + "=" * (-len(jwk["n"]) % 4)), "big")
        e = int.from_bytes(base64.urlsafe_b64decode(jwk["e"] + "=" * (-len(jwk["e"]) % 4)), "big")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise OAuthKeyError("not an RSA public JWK") from exc
    key = RSAPublicNumbers(e, n).public_key()
    if key.key_size < 2048:
        raise OAuthKeyError("the token-signing key must have 2048 bits or more")
    return key


def public_pem(key: RSAPublicKey) -> str:
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def load_public_key(path: Path) -> RSAPublicKey:
    """The token-signing key's public half (PEM), as the production API gets it: the signer
    service holds the private half."""
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except FileNotFoundError as exc:
        raise OAuthKeyError(f"token public key file not found: {path}") from exc
    except (ValueError, TypeError) as exc:
        raise OAuthKeyError(f"{path} is not a PEM public key") from exc
    if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
        raise OAuthKeyError(f"{path} is not an RSA public key of 2048 bits or more")
    return key


def write_new_key(path: Path) -> dict[str, str]:
    """Create a new RSA key file (mode 600). Returns its public JWK."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return public_jwk(key.public_key())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dmd-gateway-oauth-key",
        description="Create the RSA key the authorization server signs access tokens with.",
    )
    parser.add_argument("path", type=Path, help="where to write the key, e.g. secrets/oauth.pem")
    parser.add_argument(
        "--public",
        action="store_true",
        help="print the public half (PEM) of the existing key at PATH instead",
    )
    args = parser.parse_args(argv)
    if args.public:
        try:
            print(public_pem(load_private_key(args.path).public_key()), end="")
        except OAuthKeyError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    try:
        jwk = write_new_key(args.path)
    except FileExistsError:
        print(f"{args.path} already exists; refusing to overwrite it.", file=sys.stderr)
        return 1
    print(f"Created {args.path} (readable by its owner only).")
    print(f"Public key (JWK): {json.dumps(jwk, separators=(',', ':'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
