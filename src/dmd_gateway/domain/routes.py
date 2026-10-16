"""Payment route registry: the single source of truth for how an agent may pay (QUOTE-01..04).

The registry is loaded from ``config/payment_routes.yaml`` and resolved against the
environment once at startup. A rail is ENABLED only if all of the following hold:

  * its ``enable_env`` flag is true (1/true/yes/on);
  * its kill switch (``kill_env``) is not set;
  * it is marked ``approved`` in the registry;
  * its canonical asset identity and confirmation/finality policy are set;
  * for non-USD assets, a complete price policy is set (approved source, maximum price
    age, permitted price deviation and fallback) and its price sources are implemented;
  * its receiving destination is configured;
  * a payment adapter with its ``adapter`` name is actually implemented.

If a rail is switched on but any of the last six conditions fail, startup fails with a
``RegistryError`` rather than silently running a half-configured payment rail.

Kill switches fail safe: any value other than empty/0/false/no/off counts as "killed".
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from dmd_gateway.domain.money import parse_amount

ENV_NAME = r"^[A-Z][A-Z0-9_]*$"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


class RegistryError(RuntimeError):
    pass


class RailState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    KILLED = "killed"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssetConfig(_Strict):
    symbol: str = Field(min_length=1, max_length=16)
    decimals: int = Field(ge=0, le=36)
    asset_type: Literal["native", "token", "wrapped_token"] = "native"
    # Chain-qualified identity, e.g. "dmd:mainnet/native" or "eip155:8453/erc20:0x...".
    # Kept in this version-controlled file (not the environment) so that enabling a new
    # token contract always goes through change control. Never match by ticker alone.
    canonical_id: str | None = None
    # USD-denominated assets (e.g. USDC) are quoted 1:1 against the USD price and need no
    # conversion price source; every other asset needs a complete price policy.
    usd_denominated: bool = False


class RecipientConfig(_Strict):
    # per_intent_address: a fresh address is derived for every payment intent.
    # env: one merchant address read from the named environment variable.
    source: Literal["per_intent_address", "env"]
    env: str | None = Field(default=None, pattern=ENV_NAME)

    @model_validator(mode="after")
    def _env_required(self) -> RecipientConfig:
        if self.source == "env" and not self.env:
            raise ValueError("recipient.env is required when recipient.source is 'env'")
        return self


class ConfirmationPolicy(_Strict):
    required_confirmations: int | None = Field(default=None, ge=1)
    finality: str | None = None

    @property
    def is_set(self) -> bool:
        return self.required_confirmations is not None or bool(self.finality)


class PricePolicy(_Strict):
    """Conversion policy for non-USD assets: approved, signed, monitored source only."""

    source: str | None = None
    max_price_age_seconds: int | None = Field(default=None, ge=1)
    max_deviation_bps: int | None = Field(default=None, ge=1, le=10_000)
    fallback: Literal["refuse_quote", "secondary_source"] | None = None
    secondary_source: str | None = None

    def missing(self) -> list[str]:
        missing = [
            name
            for name in ("source", "max_price_age_seconds", "max_deviation_bps", "fallback")
            if getattr(self, name) is None
        ]
        if self.fallback == "secondary_source" and not self.secondary_source:
            missing.append("secondary_source")
        return missing


class RailConfig(_Strict):
    name: str
    stage: Literal["initial", "future"]
    approved: bool = False
    asset: AssetConfig
    network: str
    protocol: str
    scheme: str | None = None
    transfer_method: str | None = None
    adapter: str
    recipient: RecipientConfig
    confirmation_policy: ConfirmationPolicy = ConfirmationPolicy()
    price_policy: PricePolicy = PricePolicy()
    wallet_compatibility: tuple[str, ...] = ()
    enable_env: str = Field(pattern=ENV_NAME)
    kill_env: str = Field(pattern=ENV_NAME)


class CommercialPrice(_Strict):
    currency: Literal["USD"]
    amount: Decimal
    quote_required: bool = True

    @field_validator("amount", mode="before")
    @classmethod
    def _parse(cls, value: object) -> Decimal:
        if not isinstance(value, str):
            raise ValueError("commercial price amount must be a quoted decimal string")
        amount = parse_amount(value)
        if amount <= 0:
            raise ValueError("commercial price must be greater than zero")
        return amount


class ServiceConfig(_Strict):
    title: str
    description: str
    method: Literal["GET", "POST"]
    path: str
    commercial_price: CommercialPrice
    payment_options: tuple[str, ...]
    terms_version: str = Field(min_length=1, max_length=64)
    policy_urls: dict[str, str] = {}


class RegistryFile(_Strict):
    version: Literal[1]
    discovery_terms: tuple[str, ...] = ()
    services: dict[str, ServiceConfig]
    rails: dict[str, RailConfig]

    @model_validator(mode="after")
    def _cross_references(self) -> RegistryFile:
        for service_id, service in self.services.items():
            for rail_id in service.payment_options:
                if rail_id not in self.rails:
                    raise ValueError(f"service {service_id!r} references unknown rail {rail_id!r}")
        return self


def _flag_on(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


def _kill_on(value: str | None) -> bool:
    return (value or "").strip().lower() not in _FALSE


def readiness_problems(
    rail: RailConfig,
    environ: Mapping[str, str],
    available_adapters: Collection[str],
    available_price_sources: Collection[str] = (),
) -> list[str]:
    """Everything that stops a rail from being safely enabled."""
    problems: list[str] = []
    if not rail.approved:
        problems.append("rail is not approved for production use")
    if not rail.asset.canonical_id:
        problems.append("asset.canonical_id is not set")
    if not rail.confirmation_policy.is_set:
        problems.append("confirmation_policy is not set")
    if not rail.asset.usd_denominated:
        missing = rail.price_policy.missing()
        if missing:
            problems.append("price_policy is incomplete (missing " + ", ".join(missing) + ")")
        for source in (rail.price_policy.source, rail.price_policy.secondary_source):
            if source and source not in available_price_sources:
                problems.append(f"price source {source} is not implemented")
    if rail.recipient.source == "env" and not (environ.get(rail.recipient.env or "") or "").strip():
        problems.append(f"receiving address variable {rail.recipient.env} is empty")
    if rail.adapter not in available_adapters:
        problems.append(f"payment adapter {rail.adapter} is not implemented")
    return problems


class PaymentRouteRegistry:
    def __init__(self, data: RegistryFile, states: Mapping[str, RailState]) -> None:
        self._data = data
        self._states = dict(states)

    @property
    def services(self) -> Mapping[str, ServiceConfig]:
        return self._data.services

    @property
    def rails(self) -> Mapping[str, RailConfig]:
        return self._data.rails

    @property
    def discovery_terms(self) -> tuple[str, ...]:
        return self._data.discovery_terms

    def rail_state(self, rail_id: str) -> RailState:
        return self._states[rail_id]

    def enabled_rails(self, service_id: str) -> list[tuple[str, RailConfig]]:
        service = self._data.services[service_id]
        return [
            (rail_id, self._data.rails[rail_id])
            for rail_id in service.payment_options
            if self._states[rail_id] is RailState.ENABLED
        ]


def load_registry(
    path: str | Path,
    *,
    environ: Mapping[str, str],
    available_adapters: Collection[str],
    available_price_sources: Collection[str] = (),
) -> PaymentRouteRegistry:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError(f"cannot read payment route registry {path}: {exc}") from exc
    try:
        data = RegistryFile.model_validate(raw)
    except ValidationError as exc:
        raise RegistryError(f"invalid payment route registry {path}:\n{exc}") from exc

    states: dict[str, RailState] = {}
    for rail_id, rail in data.rails.items():
        if _kill_on(environ.get(rail.kill_env)):
            states[rail_id] = RailState.KILLED
        elif not _flag_on(environ.get(rail.enable_env)):
            states[rail_id] = RailState.DISABLED
        else:
            problems = readiness_problems(
                rail, environ, available_adapters, available_price_sources
            )
            if problems:
                raise RegistryError(
                    f"rail {rail_id!r} is switched on but not ready: " + "; ".join(problems)
                )
            states[rail_id] = RailState.ENABLED
    return PaymentRouteRegistry(data, states)
