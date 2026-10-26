from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from dmd_gateway.domain.routes import RailState, RegistryError, load_registry

from .conftest import REGISTRY_PATH

ALL_ADAPTERS = {"DMDNativePaymentAdapter", "X402PaymentAdapter", "SolanaPaymentAdapter"}
ALL_SOURCES = {"approved_dmd_usd_source"}


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _shipped() -> dict:
    return yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))


def _complete_dmd(data: dict) -> dict:
    rail = data["rails"]["dmd_pow_native"]
    rail["confirmation_policy"]["required_confirmations"] = 6
    rail["price_policy"] = {
        "source": "approved_dmd_usd_source",
        "max_price_age_seconds": 900,
        "max_deviation_bps": 200,
        "fallback": "refuse_quote",
    }
    return data


def test_shipped_registry_loads_with_every_rail_off() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    assert set(registry.rails) == {
        "dmd_pow_native",
        "x402_usdc_base",
        "x402_dmd_wrapped_base",
        "solana_usdc",
    }
    assert all(registry.rail_state(r) is RailState.DISABLED for r in registry.rails)
    assert registry.enabled_rails("did_registration") == []


def test_commercial_price_is_fixed_usd() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    price = registry.services["did_registration"].commercial_price
    assert price.currency == "USD"
    assert str(price.amount) == "25.00"
    assert price.quote_required


def test_shipped_dmd_rail_is_priced_by_the_mining_cost() -> None:
    environ = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"}
    registry = load_registry(
        REGISTRY_PATH,
        environ=environ,
        available_adapters=ALL_ADAPTERS,
        available_price_sources=ALL_SOURCES | {"dmd_mining_cost"},
    )
    policy = registry.rails["dmd_pow_native"].price_policy
    assert policy.source == "dmd_mining_cost"
    assert policy.fallback == "refuse_quote"
    assert policy.max_price_age_seconds == 900  # three missed refreshes refuse quotes
    # Without that price source, the rail cannot be switched on.
    with pytest.raises(RegistryError, match="price source dmd_mining_cost is not implemented"):
        load_registry(
            REGISTRY_PATH,
            environ=environ,
            available_adapters=ALL_ADAPTERS,
            available_price_sources=ALL_SOURCES - {"dmd_mining_cost"},
        )


def test_rail_cannot_be_enabled_without_an_adapter(tmp_path: Path) -> None:
    path = _write(tmp_path, _complete_dmd(_shipped()))
    environ = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"}
    with pytest.raises(RegistryError, match="DMDNativePaymentAdapter is not implemented"):
        load_registry(path, environ=environ, available_adapters=set())


def test_complete_rail_with_adapter_is_enabled(tmp_path: Path) -> None:
    path = _write(tmp_path, _complete_dmd(_shipped()))
    environ = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "TRUE"}
    registry = load_registry(
        path, environ=environ, available_adapters=ALL_ADAPTERS, available_price_sources=ALL_SOURCES
    )
    assert registry.rail_state("dmd_pow_native") is RailState.ENABLED
    assert [r for r, _ in registry.enabled_rails("did_registration")] == ["dmd_pow_native"]


@pytest.mark.parametrize("value", ["1", "true", "YES", "anything-unexpected"])
def test_kill_switch_wins_over_enable(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, _complete_dmd(_shipped()))
    environ = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true", "DMD_RAIL_DMD_POW_NATIVE_KILL": value}
    registry = load_registry(
        path, environ=environ, available_adapters=ALL_ADAPTERS, available_price_sources=ALL_SOURCES
    )
    assert registry.rail_state("dmd_pow_native") is RailState.KILLED
    assert registry.enabled_rails("did_registration") == []


@pytest.mark.parametrize("value", ["", "0", "false", "off", "No"])
def test_kill_switch_off_values(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, _complete_dmd(_shipped()))
    environ = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true", "DMD_RAIL_DMD_POW_NATIVE_KILL": value}
    registry = load_registry(
        path, environ=environ, available_adapters=ALL_ADAPTERS, available_price_sources=ALL_SOURCES
    )
    assert registry.rail_state("dmd_pow_native") is RailState.ENABLED


@pytest.mark.parametrize("value", ["", "0", "flase", "enabled"])
def test_enable_flag_must_be_explicit(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, _complete_dmd(_shipped()))
    registry = load_registry(
        path,
        environ={"DMD_RAIL_DMD_POW_NATIVE_ENABLED": value},
        available_adapters=ALL_ADAPTERS,
    )
    assert registry.rail_state("dmd_pow_native") is RailState.DISABLED


X402_SERVICES = {
    "DMD_X402_FACILITATOR_URL": "https://facilitator.test",
    "DMD_BASE_RPC_URL": "https://rpc.test",
}


def test_env_recipient_must_be_configured(tmp_path: Path) -> None:
    path = _write(tmp_path, _shipped())
    environ = {"DMD_RAIL_X402_USDC_BASE_ENABLED": "true", **X402_SERVICES}
    with pytest.raises(RegistryError, match="DMD_USDC_BASE_MERCHANT_ADDRESS is empty"):
        load_registry(
            path,
            environ=environ,
            available_adapters=ALL_ADAPTERS,
            available_price_sources=ALL_SOURCES,
        )

    environ["DMD_USDC_BASE_MERCHANT_ADDRESS"] = "0xMERCHANT"
    with pytest.raises(RegistryError, match="is not an EVM address"):
        load_registry(path, environ=environ, available_adapters=ALL_ADAPTERS)

    environ["DMD_USDC_BASE_MERCHANT_ADDRESS"] = "0x" + "ab" * 20
    registry = load_registry(
        path, environ=environ, available_adapters=ALL_ADAPTERS, available_price_sources=ALL_SOURCES
    )
    assert registry.rail_state("x402_usdc_base") is RailState.ENABLED


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda r: r.update(scheme="upto"), "scheme 'upto' is not supported"),
        (lambda r: r.update(transfer_method="permit2"), "transfer method 'permit2'"),
        (lambda r: r.update(network="solana:mainnet"), "is not an EVM network"),
        (
            lambda r: r["asset"].update(canonical_id="eip155:8453/erc20:0xTEST"),
            "canonical_id must be eip155:8453/erc20:",
        ),
        (
            lambda r: r["asset"].update(canonical_id="eip155:1/erc20:0x" + "22" * 20),
            "canonical_id must be eip155:8453/erc20:",
        ),
        (
            lambda r: r.update(confirmation_policy={"finality": "facilitator_settled"}),
            "required_confirmations is not set",
        ),
        (lambda r: r.pop("x402"), "x402 settings"),
    ],
)
def test_x402_rail_needs_a_complete_configuration(
    tmp_path: Path, change: Any, problem: str
) -> None:
    data = _shipped()
    change(data["rails"]["x402_usdc_base"])
    path = _write(tmp_path, data)
    environ = {
        "DMD_RAIL_X402_USDC_BASE_ENABLED": "true",
        "DMD_USDC_BASE_MERCHANT_ADDRESS": "0x" + "ab" * 20,
        **X402_SERVICES,
    }
    with pytest.raises(RegistryError, match=re.escape(problem)):
        load_registry(path, environ=environ, available_adapters=ALL_ADAPTERS)


@pytest.mark.parametrize("missing", sorted(X402_SERVICES))
def test_x402_rail_needs_its_facilitator_and_rpc(tmp_path: Path, missing: str) -> None:
    path = _write(tmp_path, _shipped())
    environ = {
        "DMD_RAIL_X402_USDC_BASE_ENABLED": "true",
        "DMD_USDC_BASE_MERCHANT_ADDRESS": "0x" + "ab" * 20,
        **X402_SERVICES,
    }
    environ[missing] = " "
    with pytest.raises(RegistryError, match=f"{missing} is empty"):
        load_registry(path, environ=environ, available_adapters=ALL_ADAPTERS)


def test_shipped_usdc_rail_names_circles_contract_and_domain() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=ALL_ADAPTERS)
    usdc = registry.rails["x402_usdc_base"]
    assert usdc.token_contract == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert usdc.evm_chain_id == 8453
    assert usdc.x402 is not None
    assert (usdc.x402.asset_eip712_name, usdc.x402.asset_eip712_version) == ("USD Coin", "2")
    assert usdc.confirmation_policy.required_confirmations == 5


def test_unapproved_rail_can_never_be_enabled(tmp_path: Path) -> None:
    data = _shipped()
    rail = data["rails"]["solana_usdc"]
    rail["asset"]["canonical_id"] = "solana:mainnet/spl:TEST"
    rail["confirmation_policy"] = {"finality": "finalized"}
    path = _write(tmp_path, data)
    environ = {
        "DMD_SOLANA_PAYMENTS_ENABLED": "true",
        "DMD_USDC_SOLANA_MERCHANT_ADDRESS": "SoLaNaAdDrEsS",
    }
    with pytest.raises(RegistryError, match="not approved"):
        load_registry(
            path,
            environ=environ,
            available_adapters=ALL_ADAPTERS,
            available_price_sources=ALL_SOURCES,
        )


def test_unknown_rail_reference_is_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["services"]["did_registration"]["payment_options"].append("any_crypto")
    with pytest.raises(RegistryError, match="unknown rail 'any_crypto'"):
        load_registry(_write(tmp_path, data), environ={}, available_adapters=set())


def test_unquoted_price_is_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["services"]["did_registration"]["commercial_price"]["amount"] = 25.0
    with pytest.raises(RegistryError, match="quoted decimal string"):
        load_registry(_write(tmp_path, data), environ={}, available_adapters=set())


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["rails"]["dmd_pow_native"]["enabled"] = True  # must come from the environment
    with pytest.raises(RegistryError):
        load_registry(_write(tmp_path, data), environ={}, available_adapters=set())


@pytest.mark.parametrize(
    "drop", ["source", "max_price_age_seconds", "max_deviation_bps", "fallback"]
)
def test_non_usd_rail_needs_a_complete_price_policy(tmp_path: Path, drop: str) -> None:
    data = _complete_dmd(_shipped())
    data["rails"]["dmd_pow_native"]["price_policy"][drop] = None
    environ = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"}
    with pytest.raises(RegistryError, match=f"price_policy is incomplete.*{drop}"):
        load_registry(
            _write(tmp_path, data),
            environ=environ,
            available_adapters=ALL_ADAPTERS,
            available_price_sources=ALL_SOURCES,
        )


def test_secondary_source_fallback_needs_a_secondary_source(tmp_path: Path) -> None:
    data = _complete_dmd(_shipped())
    data["rails"]["dmd_pow_native"]["price_policy"]["fallback"] = "secondary_source"
    environ = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"}
    with pytest.raises(RegistryError, match="secondary_source"):
        load_registry(
            _write(tmp_path, data),
            environ=environ,
            available_adapters=ALL_ADAPTERS,
            available_price_sources=ALL_SOURCES,
        )


def test_usd_denominated_rail_needs_no_price_policy() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    usdc = registry.rails["x402_usdc_base"]
    assert usdc.asset.usd_denominated
    assert usdc.price_policy.source is None


def test_directive_values_are_preserved() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    service = registry.services["did_registration"]
    assert service.title == "DebitMyData Agentic DID Registration"
    assert (service.method, service.path) == ("POST", "/v1/dids/registrations")
    assert "commercial fee of $25.00 USD" in service.description
    rails = registry.rails
    assert (rails["dmd_pow_native"].network, rails["dmd_pow_native"].protocol) == (
        "dmd:mainnet",
        "dmd_native",
    )
    assert rails["x402_usdc_base"].network == "eip155:8453"
    assert rails["x402_usdc_base"].scheme == "exact"
    assert rails["x402_usdc_base"].recipient.env == "DMD_USDC_BASE_MERCHANT_ADDRESS"
    assert rails["x402_dmd_wrapped_base"].transfer_method == "permit2"
    assert rails["x402_dmd_wrapped_base"].enable_env == "DMD_WRAPPED_DMD_ENABLED"
    assert rails["solana_usdc"].enable_env == "DMD_SOLANA_PAYMENTS_ENABLED"
    assert rails["solana_usdc"].recipient.env == "DMD_USDC_SOLANA_MERCHANT_ADDRESS"
    assert len(registry.discovery_terms) == 21


def test_price_source_must_be_implemented(tmp_path: Path) -> None:
    path = _write(tmp_path, _complete_dmd(_shipped()))
    environ = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"}
    with pytest.raises(RegistryError, match="price source approved_dmd_usd_source is not"):
        load_registry(path, environ=environ, available_adapters=ALL_ADAPTERS)


def test_terms_version_is_set() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    assert registry.services["did_registration"].terms_version == "2026-10-25"


# --------------------------------------------------------------------------- donations

USDC_ENV = {
    "DMD_RAIL_X402_USDC_BASE_ENABLED": "true",
    "DMD_USDC_BASE_MERCHANT_ADDRESS": "0x" + "11" * 20,
    "DMD_X402_FACILITATOR_URL": "https://facilitator.test",
    "DMD_BASE_RPC_URL": "https://rpc.test",
}


def test_shipped_inference_donation() -> None:
    registry = load_registry(REGISTRY_PATH, environ={}, available_adapters=set())
    donation = registry.donations["inference_donation"]
    assert donation.path == "/v1/donations/inference"
    assert (str(donation.amount.minimum), str(donation.amount.maximum)) == ("0.50", "1000.00")
    assert donation.payment_options == ("x402_usdc_base",)
    assert donation.recipients == {"x402_usdc_base": "DMD_USDC_BASE_INFERENCE_ADDRESS"}
    assert donation.terms_version == "2026-10-25"
    assert registry.donation_rails("inference_donation") == []  # every rail is off


def test_donation_is_offered_once_its_rail_and_wallet_are_ready() -> None:
    inference = "0x" + "1d" * 20
    registry = load_registry(REGISTRY_PATH, environ=USDC_ENV, available_adapters=ALL_ADAPTERS)
    assert registry.donation_rails("inference_donation") == []  # no receiving wallet yet
    environ = {**USDC_ENV, "DMD_USDC_BASE_INFERENCE_ADDRESS": inference}
    registry = load_registry(REGISTRY_PATH, environ=environ, available_adapters=ALL_ADAPTERS)
    ((rail_id, _, recipient),) = registry.donation_rails("inference_donation")
    assert (rail_id, recipient) == ("x402_usdc_base", inference)
    # The DID payment wallet is not the donation wallet.
    assert registry.rails["x402_usdc_base"].recipient.env == "DMD_USDC_BASE_MERCHANT_ADDRESS"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda d: d.update(
                payment_options=["dmd_pow_native"], recipients={"dmd_pow_native": "X"}
            ),
            "only x402 rails in a USD",
        ),
        (
            lambda d: d.update(payment_options=["nope"], recipients={"nope": "X"}),
            "unknown rail 'nope'",
        ),
        (lambda d: d.update(path="/v1/dids/registrations"), "path"),
        (lambda d: d["amount"].update(minimum="0"), "greater than zero"),
        (lambda d: d["amount"].update(minimum="2000.00"), "minimum"),
        (lambda d: d["amount"].update(minimum="1.005"), "decimals"),
        (lambda d: d["amount"].update(currency="EUR"), "currency"),
        (lambda d: d["policy_urls"].update(terms="http://example.com"), "policy_urls.terms"),
        (lambda d: d.update(recipients={"other": "X"}), "recipients"),
        (lambda d: d.update(other=1), "other"),
    ],
)
def test_donation_configuration_is_checked(tmp_path: Path, change: Any, message: str) -> None:
    data = _shipped()
    change(data["donations"]["inference_donation"])
    with pytest.raises((RegistryError, ValueError), match=re.escape(message)):
        load_registry(_write(tmp_path, data), environ={}, available_adapters=set())


def test_a_donation_cannot_take_the_id_of_a_service(tmp_path: Path) -> None:
    data = _shipped()
    data["donations"]["did_registration"] = data["donations"].pop("inference_donation")
    with pytest.raises(RegistryError, match="has the ID of a service"):
        load_registry(_write(tmp_path, data), environ={}, available_adapters=set())
