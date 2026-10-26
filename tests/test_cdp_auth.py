"""Coinbase (CDP) API-key authentication for the x402 facilitator: the JWT each request
carries, the key formats, the facilitator client sending it, and the registry checks."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from dmd_gateway.adapters.cdp_auth import TOKEN_LIFETIME_SECONDS, CdpApiKeyAuth
from dmd_gateway.adapters.x402_facilitator import FacilitatorClient, FacilitatorUnavailable
from dmd_gateway.domain.routes import load_registry, x402_problems

from .conftest import REGISTRY_PATH

CDP = "https://api.cdp.coinbase.com/platform/v2/x402"
KEY_ID = "2f8e1c5a-4b7d-4e2a-9c3f-1a2b3c4d5e6f"
NOW = 1_800_000_000


def b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def parse(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    header, claims, signature = token.split(".")
    return (
        json.loads(b64url_decode(header)),
        json.loads(b64url_decode(claims)),
        f"{header}.{claims}".encode(),
        b64url_decode(signature),
    )


def ed25519_secret() -> tuple[str, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(seed + public).decode(), key


def ecdsa_secret(curve: ec.EllipticCurve | None = None) -> tuple[str, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(curve or ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return pem, key


def test_ed25519_token_has_the_cdp_format_and_a_valid_signature() -> None:
    secret, key = ed25519_secret()
    auth = CdpApiKeyAuth(KEY_ID, secret, clock=lambda: NOW)
    header, claims, signed, signature = parse(auth.token("post", f"{CDP}/settle"))

    assert header == {"alg": "EdDSA", "kid": KEY_ID, "typ": "JWT", "nonce": header["nonce"]}
    assert len(header["nonce"]) == 16 and int(header["nonce"], 16) >= 0
    # The claims Coinbase's own SDK (cdp-sdk) puts in its tokens.
    assert claims == {
        "sub": KEY_ID,
        "iss": "cdp",
        "aud": None,
        "nbf": NOW,
        "exp": NOW + TOKEN_LIFETIME_SECONDS,
        "uris": ["POST api.cdp.coinbase.com/platform/v2/x402/settle"],
    }
    assert TOKEN_LIFETIME_SECONDS == 120
    key.public_key().verify(signature, signed)  # raises if invalid


def test_each_token_is_new_and_names_one_request() -> None:
    secret, _ = ed25519_secret()
    auth = CdpApiKeyAuth(KEY_ID, secret, clock=lambda: NOW)
    first, second = auth.token("POST", f"{CDP}/verify"), auth.token("POST", f"{CDP}/verify")
    assert parse(first)[0]["nonce"] != parse(second)[0]["nonce"]
    assert parse(auth.token("GET", f"{CDP}/supported"))[1]["uris"] == [
        "GET api.cdp.coinbase.com/platform/v2/x402/supported"
    ]
    assert auth("POST", f"{CDP}/verify")["Authorization"].startswith("Bearer ey")


@pytest.mark.parametrize("escaped", [False, True], ids=["multi-line", "with \\n escapes"])
def test_legacy_ecdsa_key_signs_es256(escaped: bool) -> None:
    pem, key = ecdsa_secret()
    secret = pem.replace("\n", "\\n") if escaped else pem
    auth = CdpApiKeyAuth(f"organizations/org/apiKeys/{KEY_ID}", secret, clock=lambda: NOW)
    header, claims, signed, signature = parse(auth.token("POST", f"{CDP}/verify"))
    assert header["alg"] == "ES256"
    assert claims["sub"] == header["kid"] == f"organizations/org/apiKeys/{KEY_ID}"
    assert len(signature) == 64  # JWS form: r and s, 32 bytes each
    der = encode_dss_signature(
        int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    )
    key.public_key().verify(der, signed, ec.ECDSA(hashes.SHA256()))


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def _damaged() -> str:
    secret, _ = ed25519_secret()
    raw = bytearray(base64.b64decode(secret))
    raw[40] ^= 1  # flip a bit in the public half
    return base64.b64encode(bytes(raw)).decode()


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "empty"),
        ("not base64!", "neither base64"),
        (base64.b64encode(b"x" * 32).decode(), "64 bytes"),
        (_damaged(), "damaged"),
        (_rsa_pem(), "P-256"),
        (ecdsa_secret(ec.SECP384R1())[0], "P-256"),
        ("-----BEGIN EC PRIVATE KEY-----\nbroken\n-----END EC PRIVATE KEY-----", "valid PEM"),
    ],
    ids=["empty", "garbage", "short", "damaged", "rsa", "p384", "broken pem"],
)
def test_unusable_secrets_are_refused_without_showing_them(secret: str, message: str) -> None:
    with pytest.raises(ValueError, match=message) as error:
        CdpApiKeyAuth(KEY_ID, secret)
    if len(secret) > 20:
        assert secret[10:30] not in str(error.value)


def test_key_is_never_shown_and_only_https_is_signed() -> None:
    secret, _ = ed25519_secret()
    auth = CdpApiKeyAuth(KEY_ID, secret)
    assert secret not in repr(auth) and KEY_ID in repr(auth)
    with pytest.raises(ValueError, match="https"):
        auth.token("POST", "http://api.cdp.coinbase.com/platform/v2/x402/settle")
    with pytest.raises(ValueError, match="key ID"):
        CdpApiKeyAuth(" ", secret)


# --------------------------------------------------------------------------- the client


def _recording_client(auth: Any) -> tuple[FacilitatorClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/supported"):
            kinds = [{"x402Version": 2, "scheme": "exact", "network": "eip155:8453"}]
            return httpx.Response(200, json={"kinds": kinds})
        if path.endswith("/verify"):
            return httpx.Response(200, json={"isValid": True, "payer": "0xabc"})
        return httpx.Response(
            200, json={"success": True, "transaction": "0x" + "1" * 64, "network": "eip155:8453"}
        )

    client = FacilitatorClient(CDP + "/", transport=httpx.MockTransport(handler), auth=auth)
    return client, seen


def test_facilitator_client_signs_every_request_for_its_own_path() -> None:
    secret, key = ed25519_secret()
    client, seen = _recording_client(CdpApiKeyAuth(KEY_ID, secret))
    assert client.authenticated
    assert client.supports("exact", "eip155:8453")
    assert client.verify({"x402Version": 2}, {"scheme": "exact"}).is_valid
    assert client.settle({"x402Version": 2}, {"scheme": "exact"}).success
    assert [f"{r.method} {r.url.path}" for r in seen] == [
        "GET /platform/v2/x402/supported",
        "POST /platform/v2/x402/verify",
        "POST /platform/v2/x402/settle",
    ]
    for request in seen:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        _, claims, signed, signature = parse(token)
        assert claims["uris"] == [f"{request.method} api.cdp.coinbase.com{request.url.path}"]
        key.public_key().verify(signature, signed)


def test_facilitator_without_a_key_sends_no_authorization() -> None:
    client, seen = _recording_client(None)
    assert not client.authenticated
    client.verify({}, {})
    assert "Authorization" not in seen[0].headers


def test_broken_credentials_make_the_facilitator_unavailable() -> None:
    def broken(method: str, url: str) -> dict[str, str]:
        raise ValueError("CDP requests must use an https URL")

    client, seen = _recording_client(broken)
    with pytest.raises(FacilitatorUnavailable, match="credentials unusable"):
        client.verify({}, {})
    assert seen == []


# --------------------------------------------------------------------------- the registry


def _x402_rail() -> Any:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    return registry.rails["x402_usdc_base"]


BASE_ENV = {
    "DMD_BASE_RPC_URL": "https://base-rpc.example",
    "DMD_USDC_BASE_MERCHANT_ADDRESS": "0x67C11dc63B45b916213189BF127094d4662a2eC3",
}


def test_cdp_facilitator_needs_a_usable_key() -> None:
    rail = _x402_rail()
    secret, _ = ed25519_secret()
    cdp = {**BASE_ENV, "DMD_X402_FACILITATOR_URL": CDP}

    problems = x402_problems(rail, cdp)
    assert any("CDP facilitator needs an API key" in p for p in problems)
    assert x402_problems(rail, {**cdp, "DMD_X402_CDP_API_KEY_ID": KEY_ID}) == [
        "set both DMD_X402_CDP_API_KEY_ID and DMD_X402_CDP_API_KEY_SECRET, or neither"
    ]
    damaged = _damaged()
    problems = x402_problems(
        rail,
        {**cdp, "DMD_X402_CDP_API_KEY_ID": KEY_ID, "DMD_X402_CDP_API_KEY_SECRET": damaged},
    )
    assert problems == [
        "DMD_X402_CDP_API_KEY_SECRET: the CDP API key secret is damaged "
        "(its public half does not match)"
    ]
    ready = {**cdp, "DMD_X402_CDP_API_KEY_ID": KEY_ID, "DMD_X402_CDP_API_KEY_SECRET": secret}
    assert x402_problems(rail, ready) == []
    # A facilitator without accounts (the x402.org test one) needs no key.
    assert (
        x402_problems(
            rail, {**BASE_ENV, "DMD_X402_FACILITATOR_URL": "https://x402.org/facilitator"}
        )
        == []
    )
