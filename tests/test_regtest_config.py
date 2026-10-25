"""The regtest registry and the test-only price source."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dmd_gateway.domain.routes import RegistryError
from dmd_gateway.pricing.base import PriceUnavailable
from dmd_gateway.pricing.static_test import StaticTestPriceSource
from dmd_gateway.runtime import build_runtime
from dmd_gateway.settings import Settings

from .conftest import REGISTRY_PATH, REPO_ROOT, UNREACHABLE_DB

REGTEST_PATH = REPO_ROOT / "config" / "payment_routes.regtest.yaml"
ENABLE_DMD = {"DMD_RAIL_DMD_POW_NATIVE_ENABLED": "true"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _settings(path: Path, environment: str = "development") -> Settings:
    extra = {}
    if environment != "development":
        extra = {
            "database_url": "postgresql+psycopg://app:secret@db.internal/gw",
            "public_base_url": "https://api.example.test",
        }
    return Settings(
        _env_file=None,
        environment=environment,
        route_registry_path=path,
        **({"database_url": UNREACHABLE_DB} | extra),
    )


TEST_RAILS = {"dmd_pow_native", "x402_usdc_base"}
ENABLE_USDC = {
    "DMD_RAIL_X402_USDC_BASE_ENABLED": "true",
    "DMD_USDC_BASE_MERCHANT_ADDRESS": "0x" + "ab" * 20,
    "DMD_X402_FACILITATOR_URL": "https://facilitator.test",
    "DMD_BASE_RPC_URL": "https://rpc.test",
}


def test_regtest_registry_differs_only_in_the_test_network_rails() -> None:
    main, regtest = _load(REGISTRY_PATH), _load(REGTEST_PATH)
    assert regtest["services"] == main["services"]
    assert regtest["discovery_terms"] == main["discovery_terms"]
    for rail_id in main["rails"]:
        if rail_id not in TEST_RAILS:
            assert regtest["rails"][rail_id] == main["rails"][rail_id]
    dmd = regtest["rails"]["dmd_pow_native"]
    assert dmd["network"] == "dmd:regtest"
    assert dmd["asset"]["canonical_id"] == "dmd:regtest/native"
    assert dmd["confirmation_policy"]["required_confirmations"] == 6
    assert dmd["price_policy"]["source"] == "static_test_price"

    usdc, main_usdc = regtest["rails"]["x402_usdc_base"], main["rails"]["x402_usdc_base"]
    assert usdc["network"] == "eip155:84532"
    assert usdc["asset"]["canonical_id"] == (
        "eip155:84532/erc20:0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    )
    assert usdc["x402"]["asset_eip712_name"] == "USDC"
    # Everything else about the rail is the same as in production.
    same = {"name", "protocol", "scheme", "transfer_method", "adapter", "recipient"}
    assert {key: usdc[key] for key in same} == {key: main_usdc[key] for key in same}
    assert usdc["enable_env"] == main_usdc["enable_env"]


def test_base_sepolia_rail_can_be_enabled_outside_production() -> None:
    runtime = build_runtime(_settings(REGTEST_PATH, "staging"), ENABLE_USDC)
    assert list(runtime.adapters) == ["x402_usdc_base"]


def test_test_networks_are_refused_in_production() -> None:
    with pytest.raises(RegistryError, match="test network eip155:84532"):
        build_runtime(_settings(REGTEST_PATH, "production"), ENABLE_USDC)


def test_regtest_dmd_rail_can_be_enabled_in_development() -> None:
    runtime = build_runtime(_settings(REGTEST_PATH), ENABLE_DMD)
    assert list(runtime.adapters) == ["dmd_pow_native"]


def test_test_price_source_is_refused_in_production() -> None:
    with pytest.raises(RegistryError, match="price source static_test_price is not implemented"):
        build_runtime(_settings(REGTEST_PATH, "production"), ENABLE_DMD)


def test_test_price_source_is_refused_on_mainnet(tmp_path: Path) -> None:
    data = _load(REGTEST_PATH)
    data["rails"]["dmd_pow_native"]["network"] = "dmd:mainnet"
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RegistryError, match="test-only price source on mainnet"):
        build_runtime(_settings(path), ENABLE_DMD)


def test_mainnet_dmd_rail_uses_the_mining_cost_price() -> None:
    runtime = build_runtime(_settings(REGISTRY_PATH), ENABLE_DMD)
    assert runtime.registry.rails["dmd_pow_native"].price_policy.source == "dmd_mining_cost"
    assert "dmd_mining_cost" in runtime.price_sources


def test_static_price_needs_a_value() -> None:
    with pytest.raises(PriceUnavailable):
        StaticTestPriceSource({}).get_usd_price("DMD")
    with pytest.raises(PriceUnavailable):
        StaticTestPriceSource({"DMD_STATIC_TEST_PRICE_USD": "0"}).get_usd_price("DMD")
    price = StaticTestPriceSource({"DMD_STATIC_TEST_PRICE_USD": "0.20"}).get_usd_price("DMD")
    assert str(price.usd_per_unit) == "0.20"
