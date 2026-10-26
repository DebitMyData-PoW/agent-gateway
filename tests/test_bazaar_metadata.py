"""What the x402 Bazaar receives about the paid registration route: limits and content rules
from Coinbase's listing requirements, checked without a database."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from dmd_gateway.domain.ids import (
    EXAMPLE_REGISTRATION_INTENT_ID,
    REGISTRATION_INTENT,
    REGISTRATION_INTENT_ID_PATTERN,
    new_id,
)
from dmd_gateway.services.x402_payments import (
    MAX_DESCRIPTION_LENGTH,
    bazaar_extension,
    resource_info,
)

BASE = "https://api.debitmydata.io"
PATH = "/v1/dids/registrations"
TITLE = "DebitMyData Agentic DID Registration"


def test_description_is_short_and_describes_instead_of_instructing() -> None:
    description = resource_info(BASE, PATH, TITLE)["description"]
    assert len(description) <= MAX_DESCRIPTION_LENGTH == 500
    # Listings are refused for text that could instruct an agent: no second person, and no
    # sentence that starts with an order.
    assert not re.search(r"\b(you|your)\b", description, re.IGNORECASE)
    for sentence in re.split(r"(?<=\.)\s+", description):
        first = sentence.split(" ", 1)[0].lower()
        assert first not in {"call", "send", "request", "prove", "use", "pay", "ignore", "do"}


def test_example_input_is_valid_against_the_declared_schema() -> None:
    bazaar = bazaar_extension(BASE)
    input_schema = bazaar["schema"]["properties"]["input"]
    Draft202012Validator.check_schema(bazaar["schema"])
    assert not list(Draft202012Validator(input_schema).iter_errors(bazaar["info"]["input"]))
    # The schema rejects bodies the endpoint would refuse.
    for body in ({}, {"registration_intent_id": "dmd_did_intent_123"}, {"x": 1}):
        candidate = {**bazaar["info"]["input"], "body": body}
        assert list(Draft202012Validator(input_schema).iter_errors(candidate))


def test_example_id_is_valid_and_never_generated() -> None:
    assert re.fullmatch(REGISTRATION_INTENT_ID_PATTERN, EXAMPLE_REGISTRATION_INTENT_ID)
    assert re.fullmatch(REGISTRATION_INTENT_ID_PATTERN, new_id(REGISTRATION_INTENT))
    # Generated IDs start with the current time; the example's time is 1970.
    assert new_id(REGISTRATION_INTENT) > EXAMPLE_REGISTRATION_INTENT_ID


def test_icon_is_a_png_served_by_the_gateway(client: TestClient) -> None:
    info = resource_info(BASE, PATH, TITLE)
    assert info["iconUrl"] == f"{BASE}/favicon.png"  # Bazaar: JPG, PNG, WebP or GIF, not SVG
    response = client.get("/favicon.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert "iconUrl" not in resource_info("http://localhost:8000", PATH, TITLE)
