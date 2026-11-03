"""Products bought for a DID that already exists, against PostgreSQL with a scripted chain.

The BottyBotty tiers (Entry, Standard, Premium): paid once and owned for good, one tier per
DID, a higher tier for the difference in price. And products bought for a period (a test
product, ``test_monthly``): the quote, the payment, completion with a receipt signed by
DebitMyData and an entitlement for the period bought, renewal, and everything that is refused
(a product switched off, an unknown, suspended or revoked DID, completion before payment).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text, update

from dmd_gateway.db.models import Entitlement
from dmd_gateway.domain.routes import RegistryError, load_registry
from dmd_gateway.identity.crypto import verify_proof
from dmd_gateway.identity.did import issuer_did
from dmd_gateway.signer.client import SignerUnavailable

from .conftest import REGISTRY_PATH, TEST_DATABASE_URL
from .fakes import SCRIPTED
from .test_identity_status import issue, run
from .test_quotes import ENABLED, _app_client, ready_registry  # noqa: F401
from .test_registration import ISSUER, audit_actions, clean_chain, engine, tick  # noqa: F401
from .test_signer import KEY, remote, signer_socket  # noqa: F401

FLAG = "DMD_PRODUCT_BOTTYBOTTY_PROTECTION_ENABLED"
PRODUCT = "test_monthly"  # a product bought for a period (added to the test registry below)
QUOTES = "/v1/products/test-monthly/quotes"
TIERS = {
    "bottybotty_entry": "49.00",
    "bottybotty_standard": "99.00",
    "bottybotty_premium": "199.00",
}
DMD = "dmd_pow_native"
SWITCHED_ON = {**ENABLED, FLAG: "true"}
UNKNOWN_DID = "did:web:localhost%3A8000:agents:" + "0" * 26
REASON = "Reported impersonation, under review"

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def products_registry(
    ready_registry: Path,  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """The shipped registry with, next to the BottyBotty tiers, a product bought for a period."""
    data = yaml.safe_load(ready_registry.read_text(encoding="utf-8"))
    monthly = {k: v for k, v in data["products"]["bottybotty_entry"].items() if k != "tier"}
    monthly.update(
        title="Test Monthly",
        path="/v1/products/test-monthly",
        commercial_price={"currency": "USD", "amount": "10.00"},
        period_days=30,
        entitlement=PRODUCT,
    )
    data["products"][PRODUCT] = monthly
    path = tmp_path_factory.mktemp("products") / "payment_routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def api(products_registry: Path, engine: Engine) -> Iterator[TestClient]:  # noqa: F811
    with _app_client(products_registry, engine, issuer_key=ISSUER, environ=SWITCHED_ON) as client:
        yield client


# --------------------------------------------------------------------------- helpers


def buy(api: TestClient, did: str, option: str = DMD, product: str = PRODUCT) -> httpx.Response:
    path = f"/v1/products/{product.replace('_', '-')}/quotes"
    return api.post(path, json={"did": did, "preferred_payment_option": option})


def quoted(api: TestClient, did: str, product: str = PRODUCT) -> dict[str, Any]:
    response = buy(api, did, product=product)
    assert response.status_code == 201, response.text
    return response.json()


def pay(api: TestClient, quote: dict[str, Any], txid: str | None = None) -> None:
    pid = quote["payment_intent_id"]
    SCRIPTED.pay(
        pid,
        int(quote["selected_payment_option"]["amount_base_units"]),
        txid=txid or os.urandom(32).hex(),
    )
    tick(api)
    SCRIPTED.confirm(pid, 6)
    tick(api)


def purchase(api: TestClient, purchase_id: str) -> dict[str, Any]:
    response = api.get(f"/v1/products/purchases/{purchase_id}")
    assert response.status_code == 200, response.text
    return response.json()


def complete(api: TestClient, purchase_id: str) -> httpx.Response:
    return api.post(f"/v1/products/purchases/{purchase_id}/complete")


def paid_and_completed(api: TestClient, q: dict[str, Any]) -> dict[str, Any]:
    pay(api, q)
    response = complete(api, q["purchase_id"])
    assert response.status_code == 200, response.text
    return response.json()


def bought(api: TestClient, did: str, product: str = PRODUCT) -> dict[str, Any]:
    return paid_and_completed(api, quoted(api, did, product))


def error(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    return response.json()["error"]


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def held(api: TestClient, identity_id: str) -> dict[str, dict[str, Any]]:
    rows = api.get(f"/v1/entitlements/{identity_id}").json()["entitlements"]
    return {row["entitlement"]: row for row in rows}


def set_entitlement(engine: Engine, identity_id: str, **values: Any) -> None:  # noqa: F811
    with engine.begin() as conn:
        conn.execute(
            update(Entitlement)
            .where(Entitlement.subject_id == identity_id, Entitlement.entitlement == PRODUCT)
            .values(**values)
        )


# --------------------------------------------------------------------------- configuration


def test_shipped_bottybotty_tiers_are_off_until_switched_on() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    tiers = registry.tiers("bottybotty")
    assert [
        (
            pid,
            f"{p.commercial_price.amount}",
            p.tier.rank if p.tier else None,
            p.tier and p.tier.name,
        )
        for pid, p in tiers
    ] == [
        ("bottybotty_entry", "49.00", 1, "Entry"),
        ("bottybotty_standard", "99.00", 2, "Standard"),
        ("bottybotty_premium", "199.00", 3, "Premium"),
    ]
    for product_id, product in tiers:
        assert (product.entitlement, product.period_days) == (product_id, None)  # for good
        assert list(product.payment_options) == [DMD]
        assert product.enable_env == FLAG
        assert not registry.product_on(product_id)
        assert registry.product_rails(product_id) == []
    on = load_registry(REGISTRY_PATH, environ={FLAG: "true"}, available_adapters=set())
    assert all(on.product_on(product_id) for product_id, _ in tiers)
    assert on.product_rails("bottybotty_entry") == []  # its payment option is still off


def _change(product: dict[str, Any], **values: Any) -> None:
    product.update(values)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p, d: _change(p, payment_options=["x402_usdc_base"]), "only quote-based rails"),
        (lambda p, d: _change(p, payment_options=["any_crypto"]), "unknown rail 'any_crypto'"),
        (lambda p, d: _change(p, entitlement="dmd_did_holder"), "reuses the entitlement"),
        (
            lambda p, d: d["products"].update(did_registration=dict(p)),
            "has the ID of a service",
        ),
        (lambda p, d: _change(p, period_days=0), "period_days"),
        (lambda p, d: _change(p, path="/v1/dids/protection"), "path"),
        (lambda p, d: _change(p, commercial_price={"currency": "USD", "amount": 10}), "quoted"),
        (lambda p, d: _change(p, payment_options=[]), "payment_options"),
        # Tiers: owned for good, one rank each, each one dearer than the one below.
        (lambda p, d: _change(p, period_days=30), "a tier is owned for good"),
        (lambda p, d: _change(p, tier={**p["tier"], "rank": 2}), "same rank"),
        (
            lambda p, d: _change(p, commercial_price={"currency": "USD", "amount": "120.00"}),
            "each tier must cost more than the one below",
        ),
        (lambda p, d: _change(p, enable_env="DMD_PRODUCT_OTHER_ENABLED"), "same enable_env"),
        (lambda p, d: _change(p, tier={**p["tier"], "group": "Botty Botty"}), "group"),
    ],
)
def test_configuration_mistakes_stop_the_gateway(
    tmp_path: Path, change: Callable[[dict[str, Any], dict[str, Any]], None], message: str
) -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    change(data["products"]["bottybotty_entry"], data)
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RegistryError, match=message):
        load_registry(path, environ={}, available_adapters=set())


# --------------------------------------------------------------------------- with the database


@needs_db
def test_switched_off_the_product_is_not_offered(
    products_registry: Path,
    engine: Engine,  # noqa: F811
) -> None:
    with _app_client(products_registry, engine, issuer_key=ISSUER) as off:
        assert off.get("/v1/products").json() == []
        did = issue(off)["did"]
        assert error(buy(off, did), 404)["code"] == "not_found"
        assert error(buy(off, did, product="bottybotty_entry"), 404)["code"] == "not_found"


@needs_db
def test_a_did_buys_a_product_for_a_period_and_gets_a_signed_receipt(
    api: TestClient,
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(api)
    did, iid = issued["did"], issued["identity_id"]
    offers = {offer["product_id"]: offer for offer in api.get("/v1/products").json()}
    assert set(offers) == {PRODUCT, *TIERS}
    offered = offers[PRODUCT]
    assert offered["tier"] is None
    assert (offered["entitlement"], offered["period_days"]) == (PRODUCT, 30)
    assert offered["payment_options"] == [DMD]
    assert offered["quote_url"].endswith(QUOTES)

    q = quoted(api, did)
    pur, pid = q["purchase_id"], q["payment_intent_id"]
    assert (q["did"], q["next_action"]) == (did, "send_payment")
    assert q["commercial_price"] == offered["commercial_price"]
    assert q["selected_payment_option"]["id"] == DMD
    assert q["status_url"].endswith(f"/v1/products/purchases/{pur}")

    # Nothing to complete before the payment is settled.
    refused = error(complete(api, pur), 409)
    assert (refused["code"], refused["next_action"]) == ("purchase_not_ready", "send_payment")

    SCRIPTED.pay(pid, int(q["selected_payment_option"]["amount_base_units"]), txid="c3" * 32)
    tick(api)
    assert purchase(api, pur)["next_action"] == "wait_for_confirmations"
    SCRIPTED.confirm(pid, 6)
    tick(api)
    state = purchase(api, pur)
    assert (state["status"], state["next_action"]) == ("paid", "complete_purchase")
    assert api.get(f"/v1/payments/{pid}").json()["next_action"] == "complete_purchase"

    before = datetime.now(UTC).replace(microsecond=0)
    response = complete(api, pur)
    assert response.status_code == 200, response.text
    state = response.json()
    assert (state["status"], state["next_action"]) == ("completed", "none")
    until = when(state["covers_until"])
    assert timedelta(days=30) <= until - before <= timedelta(days=30, seconds=10)

    # The receipt is signed by DebitMyData and names the DID, the product and the payment.
    receipt = state["receipt"]
    assert verify_proof(receipt, ISSUER.public_key_multibase)
    assert receipt["type"] == ["VerifiableCredential", "DebitMyDataProductReceipt"]
    assert when(receipt["validUntil"]) == until
    subject = receipt["credentialSubject"]
    assert subject["id"] == did
    product = subject["product"]
    assert (product["productId"], product["purchaseId"]) == (PRODUCT, pur)
    assert product["entitlement"] == PRODUCT
    assert product["price"] == offered["commercial_price"]
    assert when(product["coversUntil"]) == until
    payment = subject["payment"]
    assert (payment["paymentIntentId"], payment["status"]) == (pid, "settled")
    assert payment["transactions"] == ["c3" * 32]

    # The entitlement is on the DID, with its end.
    assert api.get(f"/v1/dids/{iid}").json()["entitlements"] == ["dmd_did_holder", PRODUCT]
    rows = held(api, iid)
    assert rows[PRODUCT]["status"] == "active"
    assert when(rows[PRODUCT]["expires_at"]) == until
    assert rows["dmd_did_holder"]["expires_at"] is None

    # Completing again returns the same purchase: one receipt, one grant.
    assert complete(api, pur).json()["receipt"] == receipt
    assert audit_actions(engine, pur) == [
        "product_quote.created",
        "purchase.status_changed",
        "purchase.completed",
    ]
    with engine.connect() as conn:
        grants = conn.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE action = 'entitlement.granted'"
                " AND subject_id = :i AND data->>'entitlement' = :e"
            ),
            {"i": iid, "e": PRODUCT},
        ).scalar_one()
    assert grants == 1

    # Buying again before the end extends from the end, not from today.
    second = bought(api, did)
    assert when(second["covers_until"]) == until + timedelta(days=30)
    assert when(second["receipt"]["credentialSubject"]["product"]["coversFrom"]) == until
    assert when(held(api, iid)[PRODUCT]["expires_at"]) == until + timedelta(days=30)


@needs_db
def test_only_an_active_did_can_buy(api: TestClient, engine: Engine) -> None:  # noqa: F811
    assert error(buy(api, UNKNOWN_DID), 404)["code"] == "unknown_did"
    issued = issue(api)
    did, iid = issued["did"], issued["identity_id"]
    q = quoted(api, did)
    pay(api, q)

    code, out = run(engine, "did", "suspend", did, "--reason", REASON, "--by", "Tyler")
    assert code == 0, out
    assert error(buy(api, did), 409)["code"] == "did_not_active"
    # Paid before the suspension: it waits, and completes once the DID is active again.
    assert error(complete(api, q["purchase_id"]), 409)["code"] == "did_not_active"
    assert purchase(api, q["purchase_id"])["status"] == "paid"
    code, out = run(engine, "did", "reactivate", did, "--reason", "No misuse", "--by", "Tyler")
    assert code == 0, out
    assert complete(api, q["purchase_id"]).status_code == 200

    # The product follows the DID, like its other entitlements.
    run(engine, "did", "suspend", did, "--reason", REASON, "--by", "Tyler")
    assert api.get(f"/v1/dids/{iid}").json()["entitlements"] == []
    assert held(api, iid)[PRODUCT]["status"] == "suspended"
    run(engine, "did", "reactivate", did, "--reason", "No misuse", "--by", "Tyler")
    assert PRODUCT in api.get(f"/v1/dids/{iid}").json()["entitlements"]

    run(engine, "did", "revoke", did, "--reason", "Fraud confirmed", "--by", "Tyler", "--yes")
    assert error(buy(api, did), 409)["code"] == "did_not_active"
    assert held(api, iid)[PRODUCT]["status"] == "revoked"


@needs_db
def test_an_expired_product_is_no_longer_held(
    api: TestClient,
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(api)
    did, iid = issued["did"], issued["identity_id"]
    bought(api, did)
    set_entitlement(engine, iid, expires_at=datetime.now(UTC) - timedelta(days=1))
    assert api.get(f"/v1/dids/{iid}").json()["entitlements"] == ["dmd_did_holder"]
    assert held(api, iid)[PRODUCT]["status"] == "expired"

    # Buying again starts today, not at the old end.
    before = datetime.now(UTC).replace(microsecond=0)
    again = bought(api, did)
    assert (
        timedelta(days=30) <= when(again["covers_until"]) - before <= timedelta(days=30, seconds=10)
    )
    assert held(api, iid)[PRODUCT]["status"] == "active"


@needs_db
def test_nothing_is_sold_that_cannot_be_given(api: TestClient, engine: Engine) -> None:  # noqa: F811
    issued = issue(api)
    did, iid = issued["did"], issued["identity_id"]
    bought(api, did)
    paid_first, paid_second = quoted(api, did), quoted(api, did)
    pay(api, paid_first)
    pay(api, paid_second)

    # Held for good (no end): a new quote is refused, and a purchase paid before never
    # shortens it.
    set_entitlement(engine, iid, expires_at=None)
    assert error(buy(api, did), 409)["code"] == "already_covered"
    done = complete(api, paid_first["purchase_id"]).json()
    assert done["status"] == "completed"
    assert done["covers_until"] is None
    assert "validUntil" not in done["receipt"]
    assert held(api, iid)[PRODUCT]["expires_at"] is None

    # An entitlement revoked on its own never comes back through a purchase.
    set_entitlement(engine, iid, status="revoked", revoked_at=datetime.now(UTC))
    assert error(buy(api, did), 409)["code"] == "entitlement_revoked"
    assert error(complete(api, paid_second["purchase_id"]), 409)["code"] == "entitlement_revoked"
    assert purchase(api, paid_second["purchase_id"])["status"] == "paid"


@needs_db
def test_payment_options_and_identifiers(api: TestClient) -> None:
    did = issue(api)["did"]
    x402 = error(buy(api, did, "x402_usdc_base"), 422)
    assert (x402["code"], x402["enabled_payment_options"]) == ("unknown_payment_option", [DMD])
    assert error(buy(api, did, "any_crypto"), 422)["code"] == "unknown_payment_option"
    assert api.post(QUOTES, json={"did": did}).status_code == 422
    extra = {"did": did, "preferred_payment_option": DMD, "amount": "0.01"}
    assert api.post(QUOTES, json=extra).status_code == 422
    body = {"did": did, "preferred_payment_option": DMD}
    assert error(api.post("/v1/products/no-such-thing/quotes", json=body), 404)["code"] == (
        "not_found"
    )
    assert api.get("/v1/products/purchases/not-a-purchase").status_code == 422
    unknown = "dmd_purchase_" + "0" * 26
    assert error(api.get(f"/v1/products/purchases/{unknown}"), 404)["code"] == "not_found"
    assert error(complete(api, unknown), 404)["code"] == "not_found"


@needs_db
def test_the_did_itself_names_it_in_the_public_records(api: TestClient) -> None:
    issued = issue(api)
    did, iid = issued["did"], issued["identity_id"]
    bought(api, did)
    # What BottyBotty checks: the entitlements, by the DID itself (the ':' of the host's port
    # written %3A or not, or URL-encoded once more) or by the identity ID.
    for name in (did, did.replace("%3A", ":"), quote(did, safe=":"), iid):
        response = api.get(f"/v1/entitlements/{name}")
        assert response.status_code == 200, (name, response.text)
        assert response.json()["did"] == did
        assert PRODUCT in {row["entitlement"] for row in response.json()["entitlements"]}
        assert api.get(f"/v1/dids/{name}").json()["identity_id"] == iid
    # A DID naming another host is not this gateway's, even with the same ending.
    other = did.replace("localhost%3A8000", "evil.example")
    assert error(api.get(f"/v1/entitlements/{other}"), 404)["code"] == "not_found"
    assert error(api.get(f"/v1/dids/{other}"), 404)["code"] == "not_found"
    assert error(buy(api, other), 404)["code"] == "unknown_did"
    assert api.get("/v1/entitlements/not-a-did").status_code == 422
    assert api.get("/v1/entitlements/did:web:x/../agents").status_code in (404, 422)


# --------------------------------------------------------------------------- BottyBotty tiers


@needs_db
def test_a_bottybotty_tier_is_owned_for_good_and_upgraded_for_the_difference(
    api: TestClient,
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(api)
    did, iid = issued["did"], issued["identity_id"]
    offers = {offer["product_id"]: offer for offer in api.get("/v1/products").json()}
    assert {pid: offers[pid]["commercial_price"]["amount"] for pid in TIERS} == TIERS
    assert offers["bottybotty_standard"]["tier"] == {
        "group": "bottybotty",
        "rank": 2,
        "name": "Standard",
    }
    assert {offers[pid]["period_days"] for pid in TIERS} == {None}

    # Entry, at its price, for good.
    q = quoted(api, did, "bottybotty_entry")
    assert (q["commercial_price"]["amount"], q["list_price"]["amount"]) == ("49.00", "49.00")
    assert q["upgrade_from"] is None
    entry = paid_and_completed(api, q)
    assert entry["covers_until"] is None
    receipt = entry["receipt"]
    assert verify_proof(receipt, ISSUER.public_key_multibase)
    assert "validUntil" not in receipt
    product = receipt["credentialSubject"]["product"]
    assert product["tier"] == {"group": "bottybotty", "rank": 1, "name": "Entry"}
    assert "upgradeFrom" not in product and "coversUntil" not in product
    assert held(api, iid)["bottybotty_entry"]["expires_at"] is None

    # The same tier again is refused before anything is paid.
    refused = error(buy(api, did, product="bottybotty_entry"), 409)
    assert (refused["code"], refused["owned"]) == ("already_covered", "bottybotty_entry")

    # Premium costs the difference: 199.00 - 49.00, in DMD at the quote's price (0.20 USD).
    q = quoted(api, did, "bottybotty_premium")
    assert (q["commercial_price"]["amount"], q["list_price"]["amount"]) == ("150.00", "199.00")
    assert q["upgrade_from"] == "bottybotty_entry"
    assert q["selected_payment_option"]["amount"] == "750.00000000"
    premium = paid_and_completed(api, q)
    product = premium["receipt"]["credentialSubject"]["product"]
    assert (product["entitlement"], product["upgradeFrom"]) == (
        "bottybotty_premium",
        "bottybotty_entry",
    )
    assert product["price"] == {"currency": "USD", "amount": "150.00"}
    assert product["listPrice"] == {"currency": "USD", "amount": "199.00"}
    assert product["tier"]["rank"] == 3

    # One BottyBotty tier on the DID: Entry became Premium, for good.
    rows = held(api, iid)
    assert "bottybotty_entry" not in rows
    assert (rows["bottybotty_premium"]["status"], rows["bottybotty_premium"]["expires_at"]) == (
        "active",
        None,
    )
    assert api.get(f"/v1/entitlements/{did}").json()["entitlements"][0]["entitlement"] == (
        "bottybotty_premium"
    )
    assert api.get(f"/v1/dids/{did}").json()["entitlements"] == [
        "bottybotty_premium",
        "dmd_did_holder",
    ]
    # A lower tier, or the same, is refused.
    for tier in ("bottybotty_entry", "bottybotty_standard", "bottybotty_premium"):
        assert error(buy(api, did, product=tier), 409)["owned"] == "bottybotty_premium"
    # The history is in the audit trail.
    with engine.connect() as conn:
        grants = conn.execute(
            text(
                "SELECT data->>'entitlement', data->>'upgrade_from' FROM audit_events"
                " WHERE action = 'entitlement.granted' AND subject_id = :i"
                " AND data->>'entitlement' LIKE 'bottybotty%' ORDER BY id"
            ),
            {"i": iid},
        ).all()
    assert [tuple(row) for row in grants] == [
        ("bottybotty_entry", None),
        ("bottybotty_premium", "bottybotty_entry"),
    ]


@needs_db
def test_two_tiers_paid_at_once_complete_once(
    api: TestClient,
    engine: Engine,  # noqa: F811
) -> None:
    issued = issue(api)
    did, iid = issued["did"], issued["identity_id"]
    standard, premium = (
        quoted(api, did, "bottybotty_standard"),
        quoted(api, did, "bottybotty_premium"),
    )
    assert (standard["commercial_price"]["amount"], premium["commercial_price"]["amount"]) == (
        "99.00",
        "199.00",
    )
    pay(api, standard)
    pay(api, premium)
    assert complete(api, premium["purchase_id"]).status_code == 200
    # Standard was paid too, but the DID now owns a higher tier: refunded by hand.
    refused = error(complete(api, standard["purchase_id"]), 409)
    assert (refused["code"], refused["next_action"]) == (
        "already_covered",
        "contact_support_for_refund",
    )
    assert "refunded by hand" in refused["message"]
    assert purchase(api, standard["purchase_id"])["status"] == "paid"
    assert list(held(api, iid)) == ["bottybotty_premium", "dmd_did_holder"]


@needs_db
def test_a_tier_follows_its_did(api: TestClient, engine: Engine) -> None:  # noqa: F811
    issued = issue(api)
    did, iid = issued["did"], issued["identity_id"]
    bought(api, did, "bottybotty_standard")
    run(engine, "did", "suspend", did, "--reason", REASON, "--by", "Tyler")
    assert held(api, iid)["bottybotty_standard"]["status"] == "suspended"
    assert error(buy(api, did, product="bottybotty_premium"), 409)["code"] == "did_not_active"
    run(engine, "did", "reactivate", did, "--reason", "No misuse", "--by", "Tyler")
    assert held(api, iid)["bottybotty_standard"]["status"] == "active"
    assert quoted(api, did, "bottybotty_premium")["commercial_price"]["amount"] == "100.00"
    # A tier revoked on its own never comes back, and nothing more is sold for it.
    with engine.begin() as conn:
        conn.execute(
            update(Entitlement)
            .where(Entitlement.subject_id == iid, Entitlement.entitlement == "bottybotty_standard")
            .values(status="revoked", revoked_at=datetime.now(UTC))
        )
    assert error(buy(api, did, product="bottybotty_premium"), 409)["code"] == (
        "entitlement_revoked"
    )


# --------------------------------------------------------------------------- the signer


def _receipt(did: str, purchase_id: str, payment_id: str) -> dict[str, Any]:
    return {
        "@context": ["https://www.w3.org/ns/credentials/v2"],
        "type": ["VerifiableCredential", "DebitMyDataProductReceipt"],
        "issuer": {"id": issuer_did("http://localhost:8000"), "name": "DebitMyData"},
        "credentialSubject": {
            "id": did,
            "product": {"productId": PRODUCT, "purchaseId": purchase_id},
            "payment": {"paymentIntentId": payment_id},
        },
    }


@needs_db
def test_the_signer_checks_a_product_receipt_against_the_database(
    signer_socket: Path,  # noqa: F811
    products_registry: Path,
    engine: Engine,  # noqa: F811
) -> None:
    issuer = remote(signer_socket)
    with _app_client(products_registry, engine, issuer_key=issuer, environ=SWITCHED_ON) as client:
        did = issue(client)["did"]
        q = quoted(client, did)
        pur, pid = q["purchase_id"], q["payment_intent_id"]
        now = datetime.now(UTC)
        with pytest.raises(SignerUnavailable, match="refused: the purchase is awaiting_payment"):
            issuer._sign(_receipt(did, pur, pid), now)
        pay(client, q)
        with pytest.raises(SignerUnavailable, match="names another product or DID"):
            issuer._sign(_receipt(UNKNOWN_DID, pur, pid), now)

        response = complete(client, pur)
        assert response.status_code == 200, response.text
        assert verify_proof(response.json()["receipt"], KEY.public_key_multibase)
        # Never a second receipt for the same purchase, even if the API asks.
        with pytest.raises(SignerUnavailable, match="refused: the purchase is completed"):
            issuer._sign(_receipt(did, pur, pid), now)


# --------------------------------------------------------------------------- the daily report


@needs_db
def test_the_daily_report_counts_products_apart_from_donations(
    api: TestClient,
    engine: Engine,  # noqa: F811
) -> None:
    from dmd_gateway.db.session import session_factory, transaction
    from dmd_gateway.monitoring import checks as mc
    from dmd_gateway.monitoring.reconcile import build_report
    from dmd_gateway.runtime import Runtime
    from dmd_gateway.settings import Settings

    bought(api, issue(api)["did"])
    state = api.app.state
    ctx = mc.MonitorContext(
        Settings(_env_file=None, database_url=TEST_DATABASE_URL),
        SWITCHED_ON,
        Runtime(state.registry, state.quote_context.adapters, {}),
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    with transaction(session_factory(engine)) as session:
        body = "\n".join(build_report(ctx, session, datetime.now(UTC)).lines)
    assert "- Inference donations completed: 0 (0.00 USD)" in body
    with engine.connect() as conn:
        count, total = conn.execute(
            text(
                "SELECT count(*), sum(commercial_price_amount) FROM purchases"
                " WHERE status = 'completed' AND product_id = :p"
            ),
            {"p": PRODUCT},
        ).one()
    assert count >= 1  # with the purchases of the earlier tests in this file
    assert f"- Products completed, {PRODUCT}: {count} ({total:.2f} USD)" in body
