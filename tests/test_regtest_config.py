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


def test_regtest_registry_differs_only_in_the_dmd_rail() -> None:
    main, regtest = _load(REGISTRY_PATH), _load(REGTEST_PATH)
    assert regtest["services"] == main["services"]
    assert regtest["discovery_terms"] == main["discovery_terms"]
    for rail_id in main["rails"]:
        if rail_id != "dmd_pow_native":
            assert regtest["rails"][rail_id] == main["rails"][rail_id]
    dmd = regtest["rails"]["dmd_pow_native"]
    assert dmd["network"] == "dmd:regtest"
    assert dmd["asset"]["canonical_id"] == "dmd:regtest/native"
    assert dmd["confirmation_policy"]["required_confirmations"] == 6
    assert dmd["price_policy"]["source"] == "static_test_price"


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


def test_mainnet_dmd_rail_still_waits_for_a_real_price_source() -> None:
    with pytest.raises(RegistryError, match="price_policy is incomplete"):
        build_runtime(_settings(REGISTRY_PATH), ENABLE_DMD)


def test_static_price_needs_a_value() -> None:
    with pytest.raises(PriceUnavailable):
        StaticTestPriceSource({}).get_usd_price("DMD")
    with pytest.raises(PriceUnavailable):
        StaticTestPriceSource({"DMD_STATIC_TEST_PRICE_USD": "0"}).get_usd_price("DMD")
    price = StaticTestPriceSource({"DMD_STATIC_TEST_PRICE_USD": "0.20"}).get_usd_price("DMD")
    assert str(price.usd_per_unit) == "0.20"
