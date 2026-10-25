"""The x402 adapter, the chain RPC client and the facilitator client, without a database."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from eth_utils import keccak

from dmd_gateway.adapters.base import (
    AdapterUnavailable,
    ExpectedPayment,
    PaymentDestination,
    SubmittedAuthorization,
)
from dmd_gateway.adapters.evm_rpc import (
    EvmRpcClient,
    EvmRpcUnavailable,
    MethodNotAllowed,
    require_https_or_loopback,
)
from dmd_gateway.adapters.x402 import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    X402PaymentAdapter,
    address_topic,
)
from dmd_gateway.adapters.x402_facilitator import FacilitatorClient, FacilitatorUnavailable
from dmd_gateway.domain.routes import load_registry
from dmd_gateway.services.x402_payments import (
    PaymentPayload,
    decode_payment_header,
    encode_header,
    mismatches,
)

from .conftest import REGISTRY_PATH
from .x402_fakes import (
    CHAIN,
    FACILITATOR,
    MERCHANT,
    TOKEN,
    FakeRpc,
    TestPayer,
    reset_chain,
)

ENVIRON = {
    "DMD_USDC_BASE_MERCHANT_ADDRESS": MERCHANT,
    "DMD_X402_FACILITATOR_URL": "https://facilitator.test",
    "DMD_BASE_RPC_URL": "https://rpc.test",
}


@pytest.fixture(autouse=True)
def fresh_chain() -> None:
    reset_chain()


def rail(token: str = TOKEN) -> Any:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    usdc = registry.rails["x402_usdc_base"]
    asset = usdc.asset.model_copy(update={"canonical_id": f"eip155:8453/erc20:{token}"})
    return usdc.model_copy(update={"asset": asset})


def adapter(**kwargs: Any) -> X402PaymentAdapter:
    return X402PaymentAdapter(
        "x402_usdc_base",
        rail(),
        ENVIRON,
        rpc=kwargs.get("rpc", FakeRpc(CHAIN)),
        facilitator=kwargs.get("facilitator", FACILITATOR),
    )


def settled_authorization(payer: TestPayer, to: str = MERCHANT, value: int = 25_000_000) -> dict:
    authorization = {
        "from": payer.address,
        "to": to,
        "value": str(value),
        "validAfter": "0",
        "validBefore": "9999999999",
        "nonce": "0x" + "5a" * 32,
    }
    return authorization


def expected(
    authorization: dict, *, hint: str | None = None, recipient: str = MERCHANT
) -> ExpectedPayment:
    return ExpectedPayment(
        payment_intent_id="dmd_payment_intent_TEST",
        network="eip155:8453",
        asset_canonical_id=f"eip155:8453/erc20:{TOKEN}",
        amount_base_units=25_000_000,
        destination=PaymentDestination(recipient, "shared_with_reference", "ref"),
        required_confirmations=5,
        authorizations=(
            SubmittedAuthorization(
                payer=authorization["from"].lower(),
                nonce=authorization["nonce"],
                submitted_at=datetime.now(UTC),
                transaction_hint=hint,
                search_from_block=CHAIN.tip - 20,  # a little before the settlement
            ),
        ),
    )


# --------------------------------------------------------------------------- constants


def test_event_topics_are_the_keccak_of_their_signatures() -> None:
    assert TRANSFER_TOPIC == "0x" + keccak(text="Transfer(address,address,uint256)").hex()
    assert AUTHORIZATION_USED_TOPIC == (
        "0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex()
    )
    assert address_topic("0xAbC0000000000000000000000000000000000001") == (
        "0x" + "0" * 24 + "abc0000000000000000000000000000000000001"
    )


# --------------------------------------------------------------------------- adapter


def test_requirements_follow_the_quote_and_the_token_domain() -> None:
    requirements = adapter().payment_requirements(25_000_000, MERCHANT, 600)
    assert requirements == {
        "scheme": "exact",
        "network": "eip155:8453",
        "amount": "25000000",
        "asset": TOKEN,
        "payTo": MERCHANT,
        "maxTimeoutSeconds": 300,  # never more than 5 minutes
        "extra": {"name": "USD Coin", "version": "2"},
    }
    assert adapter().payment_requirements(1, MERCHANT, -5)["maxTimeoutSeconds"] == 1


def test_destination_is_the_merchant_address_with_the_intent_as_reference() -> None:
    destination = adapter().prepare_destination("dmd_payment_intent_X")
    assert destination == PaymentDestination(
        MERCHANT, "shared_with_reference", "dmd_payment_intent_X"
    )


def test_ready_checks_chain_contract_and_facilitator() -> None:
    adapter().check_ready()
    CHAIN.chain_id = 1
    with pytest.raises(AdapterUnavailable, match="expects 8453"):
        adapter().check_ready()
    CHAIN.chain_id = 8453
    with pytest.raises(AdapterUnavailable, match="no token contract"):
        X402PaymentAdapter(
            "x402_usdc_base",
            rail("0x" + "99" * 20),
            ENVIRON,
            rpc=FakeRpc(CHAIN),
            facilitator=FACILITATOR,
        ).check_ready()
    FACILITATOR.network_down = True
    with pytest.raises(AdapterUnavailable):
        adapter().check_ready()


def test_a_settled_payment_is_seen_with_its_confirmations() -> None:
    payer = TestPayer()
    authorization = settled_authorization(payer)
    tx = CHAIN.transfer_with_authorization(authorization)
    observation = adapter().observe(expected(authorization, hint=tx))
    (output,) = observation.outputs
    assert output.transaction_reference == tx
    assert output.output_index == 1  # the Transfer log
    assert output.amount_base_units == 25_000_000
    assert output.confirmations == 6  # its block plus the 5 mined after it
    assert output.block_height == CHAIN.tip - 5
    assert abs(output.first_seen_at - datetime.now(UTC)) < timedelta(seconds=30)


def test_a_payment_is_found_from_the_chain_without_the_facilitators_answer() -> None:
    payer = TestPayer()
    authorization = settled_authorization(payer)
    tx = CHAIN.transfer_with_authorization(authorization)
    observation = adapter().observe(expected(authorization, hint=None))
    assert [o.transaction_reference for o in observation.outputs] == [tx]


def test_a_reported_transaction_that_is_not_this_payment_counts_for_nothing() -> None:
    mine, other = TestPayer(), TestPayer()
    authorization = settled_authorization(mine)
    others = {**settled_authorization(other), "nonce": "0x" + "6b" * 32}
    unrelated = CHAIN.transfer_with_authorization(others)
    observation = adapter().observe(expected(authorization, hint=unrelated))
    assert observation.outputs == ()
    assert any("does not use this payment authorization" in n for n in observation.notes)


def test_a_transfer_to_another_address_counts_for_nothing() -> None:
    payer = TestPayer()
    authorization = settled_authorization(payer, to="0x" + "44" * 20)
    tx = CHAIN.transfer_with_authorization(authorization)
    observation = adapter().observe(expected(authorization, hint=tx))
    assert observation.outputs == ()


def test_a_failed_transaction_counts_for_nothing() -> None:
    payer = TestPayer()
    authorization = settled_authorization(payer)
    tx = CHAIN.transfer_with_authorization(authorization)
    CHAIN.receipts[tx]["status"] = "0x0"
    observation = adapter().observe(expected(authorization, hint=tx))
    assert observation.outputs == ()


def test_a_reorged_block_is_reported() -> None:
    payer = TestPayer()
    authorization = settled_authorization(payer)
    tx = CHAIN.transfer_with_authorization(authorization)
    CHAIN.reorg()
    observation = adapter().observe(expected(authorization, hint=tx))
    assert observation.outputs == ()
    assert observation.reorged_transactions == (tx,)


def test_nothing_submitted_means_no_chain_reads() -> None:
    payment = replace(expected(settled_authorization(TestPayer())), authorizations=())
    observation = adapter().observe(payment)
    assert observation.outputs == ()
    assert CHAIN.calls == []


def test_observing_refuses_a_chain_rpc_on_another_chain() -> None:
    authorization = settled_authorization(TestPayer())
    CHAIN.chain_id = 84532
    with pytest.raises(AdapterUnavailable, match="changed chain"):
        adapter().observe(expected(authorization))


# --------------------------------------------------------------------------- payload checks


def payload(changes: dict[str, Any] | None = None, **accepted: Any) -> PaymentPayload:
    authorization = {
        "from": "0x" + "aa" * 20,
        "to": MERCHANT,
        "value": "25000000",
        "validAfter": "0",
        "validBefore": str(int(datetime.now(UTC).timestamp()) + 120),
        "nonce": "0x" + "01" * 32,
        **(changes or {}),
    }
    return PaymentPayload.model_validate(
        {
            "x402Version": 2,
            "accepted": {**REQUIREMENTS, **accepted},
            "payload": {"signature": "0x" + "11" * 65, "authorization": authorization},
        }
    )


REQUIREMENTS = {
    "scheme": "exact",
    "network": "eip155:8453",
    "amount": "25000000",
    "asset": TOKEN,
    "payTo": MERCHANT,
    "maxTimeoutSeconds": 120,
    "extra": {"name": "USD Coin", "version": "2"},
}


def test_a_matching_payment_has_no_problems() -> None:
    assert mismatches(payload(), REQUIREMENTS, datetime.now(UTC)) == []


def test_numbers_and_address_case_do_not_matter() -> None:
    mixed_case = "0x" + "aB" * 20
    as_numbers = payload({"value": 25_000_000, "validAfter": 0}, asset=mixed_case)
    requirements = REQUIREMENTS | {"asset": mixed_case.lower()}
    assert mismatches(as_numbers, requirements, datetime.now(UTC)) == []


@pytest.mark.parametrize(
    ("authorization", "accepted", "code"),
    [
        ({"value": "24999999"}, {}, "invalid_exact_evm_payload_authorization_value_mismatch"),
        ({"to": "0x" + "33" * 20}, {}, "invalid_exact_evm_payload_recipient_mismatch"),
        ({"validBefore": "100"}, {}, "invalid_exact_evm_payload_authorization_valid_before"),
        ({"validAfter": "9999999999"}, {}, "invalid_exact_evm_payload_authorization_valid_after"),
        ({}, {"network": "eip155:1"}, "invalid_network"),
        ({}, {"scheme": "upto"}, "invalid_scheme"),
        ({}, {"asset": "0x" + "99" * 20}, "invalid_payment_requirements"),
        ({}, {"amount": "1"}, "invalid_payment_requirements"),
        ({}, {"extra": {"assetTransferMethod": "permit2"}}, "invalid_payload"),
    ],
)
def test_payments_that_do_not_pay_this_quote(
    authorization: dict[str, Any], accepted: dict[str, Any], code: str
) -> None:
    problems = mismatches(payload(authorization, **accepted), REQUIREMENTS, datetime.now(UTC))
    assert code in [c for c, _ in problems]


@pytest.mark.parametrize(
    "authorization",
    [
        {"from": "0xnothex"},
        {"nonce": "0x01"},
        {"value": "-1"},
        {"extra_field": "x"},
    ],
)
def test_malformed_authorizations_are_refused(authorization: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        payload(authorization)


def test_header_round_trip_and_limits() -> None:
    value = {"x402Version": 2, "a": [1, 2]}
    assert decode_payment_header(encode_header(value)) == value
    urlsafe = base64.urlsafe_b64encode(json.dumps({"k": "~~~>>>"}).encode()).decode().rstrip("=")
    assert decode_payment_header(urlsafe) == {"k": "~~~>>>"}
    for bad in ("", "%%%", base64.b64encode(b"[]").decode(), "A" * 40_000):
        with pytest.raises(ValueError):
            decode_payment_header(bad)


# --------------------------------------------------------------------------- clients


def test_rpc_client_only_calls_read_methods() -> None:
    client = EvmRpcClient("https://rpc.test", transport=httpx.MockTransport(lambda r: None))
    for method in (
        "eth_sendRawTransaction",
        "eth_sign",
        "personal_unlockAccount",
        "eth_sendTransaction",
    ):
        with pytest.raises(MethodNotAllowed):
            client.call(method, [])


def test_rpc_errors_do_not_reveal_the_url() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    secret_url = "https://base.example/v2/SECRET-API-KEY"
    client = EvmRpcClient(secret_url, transport=httpx.MockTransport(refuse))
    with pytest.raises(EvmRpcUnavailable) as error:
        client.block_number()
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://sepolia.base.org", True),
        ("http://127.0.0.1:8545", True),
        ("http://localhost:4022/", True),
        ("http://sepolia.base.org", False),
        ("ftp://x", False),
        ("not a url", False),
    ],
)
def test_remote_services_need_https(url: str, ok: bool) -> None:
    if ok:
        assert require_https_or_loopback(url, "x") == url.rstrip("/")
    else:
        with pytest.raises(ValueError):
            require_https_or_loopback(url, "x")


def facilitator(handler: Any) -> FacilitatorClient:
    return FacilitatorClient("https://facilitator.test", transport=httpx.MockTransport(handler))


def test_facilitator_answers_are_read_from_200_and_4xx() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["x402Version"] == 2
        assert set(body) == {"x402Version", "paymentPayload", "paymentRequirements"}
        if request.url.path.endswith("/verify"):
            return httpx.Response(400, json={"isValid": False, "invalidReason": "bad_sig"})
        return httpx.Response(
            200,
            json={"success": True, "transaction": "0x" + "ab" * 32, "network": "eip155:8453"},
        )

    client = facilitator(answer)
    verdict = client.verify({"p": 1}, {"network": "eip155:8453"})
    assert (verdict.is_valid, verdict.invalid_reason) == (False, "bad_sig")
    result = client.settle({"p": 1}, {"network": "eip155:8453"})
    assert result.success and result.transaction == "0x" + "ab" * 32
    assert result.as_payment_response() == {
        "success": True,
        "transaction": "0x" + "ab" * 32,
        "network": "eip155:8453",
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"something": "else"}),
    ],
)
def test_unusable_facilitator_answers_mean_unknown(response: httpx.Response) -> None:
    client = facilitator(lambda request: response)
    with pytest.raises(FacilitatorUnavailable):
        client.settle({}, {})
    with pytest.raises(FacilitatorUnavailable):
        client.verify({}, {})


def test_facilitator_support_is_checked_per_scheme_and_network() -> None:
    kinds = {"kinds": [{"x402Version": 2, "scheme": "exact", "network": "eip155:84532"}]}
    client = facilitator(lambda request: httpx.Response(200, json=kinds))
    assert client.supports("exact", "eip155:84532")
    assert not client.supports("exact", "eip155:8453")
    assert not client.supports("upto", "eip155:84532")
