"""The Bazaar validation script (scripts/validate_bazaar.py), against a fake validator."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_bazaar.py"
spec = importlib.util.spec_from_file_location("validate_bazaar", SCRIPT)
assert spec is not None and spec.loader is not None
validate_bazaar = importlib.util.module_from_spec(spec)
sys.modules["validate_bazaar"] = validate_bazaar
spec.loader.exec_module(validate_bazaar)


def fake_validator(answer: dict, status: int = 200) -> tuple[httpx.MockTransport, list]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=answer)

    return httpx.MockTransport(handler), seen


def test_asks_coinbase_about_the_paid_route_without_a_key() -> None:
    transport, seen = fake_validator({"valid": True, "simulation": {"outcome": "accepted"}})
    ok, _ = validate_bazaar.validate("https://api.debitmydata.io/", transport=transport)
    assert ok
    (request,) = seen
    assert str(request.url) == "https://api.cdp.coinbase.com/platform/v2/x402/validate"
    assert "Authorization" not in request.headers
    assert json.loads(request.content) == {
        "resource": "https://api.debitmydata.io/v1/dids/registrations",
        "method": "POST",
    }


@pytest.mark.parametrize(
    "answer",
    [
        {"valid": False, "simulation": {"outcome": "rejected"}, "preflight": {"failures": ["x"]}},
        {"valid": True, "simulation": {"outcome": "rejected"}},
        {"valid": True},
    ],
)
def test_anything_short_of_valid_and_accepted_fails(answer: dict) -> None:
    transport, _ = fake_validator(answer)
    ok, got = validate_bazaar.validate("https://api.debitmydata.io", transport=transport)
    assert not ok
    text = validate_bazaar.report(ok, got)
    assert "Route valid: NO" in text and "Full answer" in text


def test_only_public_https_urls() -> None:
    with pytest.raises(SystemExit, match="https"):
        validate_bazaar.resource_url("http://api.debitmydata.io")


def test_unreachable_validator_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(validate_bazaar, "validate", down)
    assert validate_bazaar.main(["https://api.debitmydata.io"]) == 2


def test_checks_every_paid_route(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def answer(base_url: str, route: str) -> tuple[bool, dict]:
        asked.append(route)
        ok = route != "/v1/donations/inference"
        return ok, {"valid": ok, "simulation": {"outcome": "accepted" if ok else "rejected"}}

    monkeypatch.setattr(validate_bazaar, "validate", answer)
    assert validate_bazaar.main(["https://api.debitmydata.io"]) == 1
    assert asked == ["/v1/dids/registrations", "/v1/donations/inference"]
    out = capsys.readouterr().out
    assert "== POST /v1/donations/inference" in out and "Route valid: NO" in out

    asked.clear()
    assert validate_bazaar.main(["https://api.debitmydata.io", "--route", ROUTE_DID]) == 0
    assert asked == [ROUTE_DID]


ROUTE_DID = "/v1/dids/registrations"


def test_donation_route_url() -> None:
    url = validate_bazaar.resource_url("https://api.debitmydata.io/", "/v1/donations/inference")
    assert url == "https://api.debitmydata.io/v1/donations/inference"
