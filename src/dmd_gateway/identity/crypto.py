"""Encodings and signatures used by controller proofs and credentials.

* base58btc / multibase and the Ed25519 ``Multikey`` form (``z6Mk...``) used in DID documents;
* controller public keys as given in quotes (``ed25519:<key>``);
* RFC 8785 JSON canonicalization (JCS);
* the W3C Data Integrity cryptosuite ``eddsa-jcs-2022`` used to sign credentials.

Signature checks use the ``cryptography`` package. Public keys are also checked here to be
valid, canonical curve points of large order, so that a weak key can never be registered as
a DID controller.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# --------------------------------------------------------------------------- base58 / multibase

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: index for index, char in enumerate(_B58_ALPHABET)}

# Multicodec prefix for an Ed25519 public key (0xed, varint-encoded).
ED25519_PUB_MULTICODEC = b"\xed\x01"


class EncodingError(ValueError):
    pass


def b58encode(data: bytes) -> str:
    zeros = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    chars = []
    while number:
        number, remainder = divmod(number, 58)
        chars.append(_B58_ALPHABET[remainder])
    return "1" * zeros + "".join(reversed(chars))


def b58decode(text: str) -> bytes:
    if not text:
        raise EncodingError("empty base58 string")
    number = 0
    for char in text:
        if char not in _B58_INDEX:
            raise EncodingError("invalid base58 character")
        number = number * 58 + _B58_INDEX[char]
    zeros = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * zeros + body


def multibase_encode(data: bytes) -> str:
    """Multibase base58btc (prefix ``z``)."""
    return "z" + b58encode(data)


def multibase_decode(text: str) -> bytes:
    if not text.startswith("z"):
        raise EncodingError("only base58btc multibase values (starting with 'z') are supported")
    return b58decode(text[1:])


# --------------------------------------------------------------------------- Ed25519 points

_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_IDENTITY = (0, 1, 1, 0)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _point_add(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    big_a = (a[1] - a[0]) * (b[1] - b[0]) % _P
    big_b = (a[1] + a[0]) * (b[1] + b[0]) % _P
    big_c = 2 * a[3] * b[3] * _D % _P
    big_d = 2 * a[2] * b[2] % _P
    e, f, g, h = big_b - big_a, big_d - big_c, big_d + big_c, big_b + big_a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _is_identity(point: tuple[int, int, int, int]) -> bool:
    x, y, z, _ = point
    return x % _P == 0 and (y - z) % _P == 0


def is_strong_ed25519_public_key(raw: bytes) -> bool:
    """True for a canonical encoding of a curve point that is not of small order."""
    if len(raw) != 32:
        return False
    value = int.from_bytes(raw, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return False
    point = (x, y, 1, x * y % _P)
    for _ in range(3):  # multiply by the cofactor 8
        point = _point_add(point, point)
    return not _is_identity(point)


# --------------------------------------------------------------------------- controller keys


class ControllerKeyError(ValueError):
    pass


SUPPORTED_KEY_TYPES = ("ed25519",)


@dataclass(frozen=True, slots=True)
class ControllerKey:
    """An Ed25519 controller public key."""

    raw: bytes

    @property
    def key_type(self) -> str:
        return "ed25519"

    @property
    def multibase(self) -> str:
        return multibase_encode(ED25519_PUB_MULTICODEC + self.raw)

    @property
    def canonical(self) -> str:
        """The form stored and shown everywhere: ``ed25519:z6Mk...``."""
        return f"ed25519:{self.multibase}"

    def verify(self, signature: bytes, message: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(self.raw).verify(signature, message)
        except InvalidSignature:
            return False
        return True


def _decode_key_material(text: str) -> bytes:
    if text.startswith("z6Mk"):
        decoded = multibase_decode(text)
        if not decoded.startswith(ED25519_PUB_MULTICODEC):
            raise ControllerKeyError("multibase key is not an Ed25519 public key")
        return decoded[len(ED25519_PUB_MULTICODEC) :]
    if len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    if len(text) in (43, 44):
        try:
            return base64.urlsafe_b64decode(_pad(text.replace("+", "-").replace("/", "_")))
        except (binascii.Error, ValueError):
            pass
    raise ControllerKeyError(
        "ed25519 keys must be a Multikey value (z6Mk...), 64 hex characters or base64"
    )


def parse_controller_key(text: str) -> ControllerKey:
    """Parse ``ed25519:<Multikey | hex | base64>`` into a validated key."""
    key_type, sep, material = text.strip().partition(":")
    if not sep or not material:
        raise ControllerKeyError("expected '<key type>:<public key>', e.g. 'ed25519:z6Mk...'")
    if key_type.lower() not in SUPPORTED_KEY_TYPES:
        raise ControllerKeyError(
            f"unsupported controller key type {key_type!r}; supported: "
            + ", ".join(SUPPORTED_KEY_TYPES)
        )
    try:
        raw = _decode_key_material(material)
    except EncodingError as exc:
        raise ControllerKeyError(str(exc)) from exc
    if len(raw) != 32:
        raise ControllerKeyError("an ed25519 public key is exactly 32 bytes")
    if not is_strong_ed25519_public_key(raw):
        raise ControllerKeyError("not a valid ed25519 public key")
    return ControllerKey(raw)


def _pad(text: str) -> str:
    return text + "=" * (-len(text) % 4)


def decode_signature(text: str) -> bytes:
    """An Ed25519 signature given as base64url, base64 or hex (64 bytes)."""
    text = text.strip()
    signature: bytes | None = None
    if len(text) == 128:
        try:
            signature = bytes.fromhex(text)
        except ValueError:
            signature = None
    if signature is None and len(text) in (86, 88):
        try:
            signature = base64.urlsafe_b64decode(_pad(text.replace("+", "-").replace("/", "_")))
        except (binascii.Error, ValueError):
            signature = None
    if signature is None or len(signature) != 64:
        raise EncodingError("signature must be 64 bytes, as base64url, base64 or hex")
    return signature


def encode_signature(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------- JCS (RFC 8785)

_MAX_SAFE_INTEGER = 2**53 - 1


def canonical_json(value: Any) -> bytes:
    """RFC 8785 canonical JSON. Floats are refused: amounts are always strings here."""
    return _jcs(value).encode("utf-8")


def _jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError("integer outside the I-JSON safe range")
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise ValueError("object keys must be strings")
        items = sorted(value.items(), key=lambda item: item[0].encode("utf-16-be"))
        members = (json.dumps(key, ensure_ascii=False) + ":" + _jcs(item) for key, item in items)
        return "{" + ",".join(members) + "}"
    raise ValueError(f"cannot canonicalize {type(value).__name__}")


# --------------------------------------------------------------------------- eddsa-jcs-2022

CRYPTOSUITE = "eddsa-jcs-2022"


class ProofError(ValueError):
    pass


def xml_datetime(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_data(unsecured: Mapping[str, Any], proof_config: Mapping[str, Any]) -> bytes:
    proof_hash = hashlib.sha256(canonical_json(proof_config)).digest()
    document_hash = hashlib.sha256(canonical_json(unsecured)).digest()
    return proof_hash + document_hash


def add_proof(
    document: Mapping[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    verification_method: str,
    created: datetime,
    proof_purpose: str = "assertionMethod",
) -> dict[str, Any]:
    """Return ``document`` with a Data Integrity proof (``eddsa-jcs-2022``) attached."""
    unsecured = {key: value for key, value in document.items() if key != "proof"}
    if "@context" not in unsecured:
        raise ProofError("document has no @context")
    options = {
        "type": "DataIntegrityProof",
        "cryptosuite": CRYPTOSUITE,
        "created": xml_datetime(created),
        "verificationMethod": verification_method,
        "proofPurpose": proof_purpose,
    }
    proof_config = {**options, "@context": unsecured["@context"]}
    signature = private_key.sign(_hash_data(unsecured, proof_config))
    return {**unsecured, "proof": {**options, "proofValue": multibase_encode(signature)}}


def verify_proof(secured: Mapping[str, Any], public_key_multibase: str) -> bool:
    """Check an ``eddsa-jcs-2022`` proof against an Ed25519 Multikey public key."""
    proof = secured.get("proof")
    if not isinstance(proof, Mapping):
        return False
    if proof.get("type") != "DataIntegrityProof" or proof.get("cryptosuite") != CRYPTOSUITE:
        return False
    proof_value = proof.get("proofValue")
    if not isinstance(proof_value, str):
        return False
    unsecured = {key: value for key, value in secured.items() if key != "proof"}
    options = {key: value for key, value in proof.items() if key != "proofValue"}
    if "@context" in options:
        context = options["@context"]
        document_context = unsecured.get("@context")
        if not isinstance(context, list) or not isinstance(document_context, list):
            return False
        if document_context[: len(context)] != context:
            return False
    else:
        options["@context"] = unsecured.get("@context")
    try:
        signature = multibase_decode(proof_value)
        key = multibase_decode(public_key_multibase)
        if not key.startswith(ED25519_PUB_MULTICODEC):
            return False
        controller = ControllerKey(key[len(ED25519_PUB_MULTICODEC) :])
        return controller.verify(signature, _hash_data(unsecured, options))
    except (EncodingError, ValueError):
        return False
