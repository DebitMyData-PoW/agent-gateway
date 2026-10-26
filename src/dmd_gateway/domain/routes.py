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
``RegistryError`` rather than silently running a half-configured payment rail. x402 rails
must also name the token's EIP-712 domain and have their facilitator and chain RPC
addresses configured (see ``x402_problems``).

Kill switches fail safe: any value other than empty/0/false/no/off counts as "killed".
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from datetime import date
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

# Networks whose coins have no value. A rail on one of them never runs in production.
TEST_NETWORKS = frozenset(
    {"dmd:regtest", "dmd:testnet", "dmd:testnet4", "dmd:signet", "eip155:84532"}
)
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_EVM_NETWORK = re.compile(r"^eip155:([1-9][0-9]{0,18})$")
# Transfer methods of the x402 "exact" scheme that the gateway implements.
X402_TRANSFER_METHODS = frozenset({"eip3009"})


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


class X402Config(_Strict):
    """What an x402 rail needs beyond the common fields.

    The token's EIP-712 domain (name and version) is part of what the payer signs, so it is
    kept here under change control with the contract address. The facilitator (verifies and
    broadcasts payments) and the chain RPC (used to check settlements independently) are
    deployment addresses read from the environment, since RPC addresses often carry an API
    key.
    """

    asset_eip712_name: str = Field(min_length=1, max_length=64)
    asset_eip712_version: str = Field(min_length=1, max_length=16)
    facilitator_url_env: str = Field(pattern=ENV_NAME)
    rpc_url_env: str = Field(pattern=ENV_NAME)
    # API key of a facilitator that needs one (Coinbase's CDP facilitator): its ID and secret.
    facilitator_key_id_env: str | None = Field(default=None, pattern=ENV_NAME)
    facilitator_key_secret_env: str | None = Field(default=None, pattern=ENV_NAME)


CDP_FACILITATOR_HOST = "api.cdp.coinbase.com"


def facilitator_credentials(x402: X402Config, environ: Mapping[str, str]) -> tuple[str, str] | None:
    """The facilitator API key (ID, secret) from the environment, or None if none is set."""
    key_id = (environ.get(x402.facilitator_key_id_env or "") or "").strip()
    secret = (environ.get(x402.facilitator_key_secret_env or "") or "").strip()
    return (key_id, secret) if key_id or secret else None


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
    x402: X402Config | None = None
    wallet_compatibility: tuple[str, ...] = ()
    enable_env: str = Field(pattern=ENV_NAME)
    kill_env: str = Field(pattern=ENV_NAME)

    @property
    def evm_chain_id(self) -> int | None:
        match = _EVM_NETWORK.match(self.network)
        return int(match.group(1)) if match else None

    @property
    def token_contract(self) -> str | None:
        """The ERC-20 contract from ``canonical_id`` (``eip155:<chain>/erc20:<address>``)."""
        if self.evm_chain_id is None or not self.asset.canonical_id:
            return None
        prefix = f"{self.network}/erc20:"
        if not self.asset.canonical_id.startswith(prefix):
            return None
        address = self.asset.canonical_id[len(prefix) :]
        return address if EVM_ADDRESS.match(address) else None


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


def _check_policy_urls(value: dict[str, str]) -> dict[str, str]:
    # A path ("/policies/terms") is a page served by the gateway itself; anything else is a
    # full https URL.
    for key, url in value.items():
        local = url.startswith("/") and not url.startswith("//")
        if not (local or url.startswith("https://")):
            raise ValueError(f"policy_urls.{key} must be a path (/...) or an https URL")
    return value


class ServiceConfig(_Strict):
    title: str
    description: str
    method: Literal["GET", "POST"]
    path: str
    commercial_price: CommercialPrice
    payment_options: tuple[str, ...]
    terms_version: str = Field(min_length=1, max_length=64)
    policy_urls: dict[str, str] = {}

    @field_validator("policy_urls")
    @classmethod
    def _policy_urls(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_policy_urls(value)


class DonationAmount(_Strict):
    """The range a donor may choose from, in USD (quoted decimal strings)."""

    currency: Literal["USD"]
    minimum: Decimal
    maximum: Decimal

    @field_validator("minimum", "maximum", mode="before")
    @classmethod
    def _parse(cls, value: object) -> Decimal:
        if not isinstance(value, str):
            raise ValueError('amounts must be quoted decimal strings, e.g. "0.50"')
        amount = parse_amount(value)
        if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError("amounts must be greater than zero, with at most 2 decimals")
        return amount

    @model_validator(mode="after")
    def _ordered(self) -> DonationAmount:
        if self.minimum > self.maximum:
            raise ValueError("minimum must not be greater than maximum")
        return self


class DonationConfig(_Strict):
    """A donation the donor sizes: no quote, paid in one call (x402 rails only)."""

    title: str
    # The x402 Bazaar lists descriptions of 500 characters at most.
    description: str = Field(max_length=500)
    method: Literal["POST"]
    path: str = Field(pattern=r"^/v1/donations/[a-z][a-z0-9-]{1,40}$")
    amount: DonationAmount
    payment_options: tuple[str, ...] = Field(min_length=1)
    # Receiving address of each payment option, by environment variable name: donations go
    # to their own wallet, not to the rail's default (DID) receiving address.
    recipients: dict[str, str]
    terms_version: str = Field(min_length=1, max_length=64)
    policy_urls: dict[str, str] = {}

    @field_validator("policy_urls")
    @classmethod
    def _policy_urls(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_policy_urls(value)

    @model_validator(mode="after")
    def _recipients(self) -> DonationConfig:
        if set(self.recipients) != set(self.payment_options):
            raise ValueError("recipients must name one environment variable per payment option")
        for env in self.recipients.values():
            if not re.match(ENV_NAME, env):
                raise ValueError(f"recipient {env!r} is not an environment variable name")
        return self


class MiningCostParameters(_Strict):
    """Approved inputs of the DMD mining-cost price (DEC-01), reviewed every quarter.

    The electricity price is what miners typically pay (industrial rates where mining takes
    place), not an average of household prices; the efficiency is the energy a typical
    SHA-256 machine uses per terahash, a mix of current and older machines.
    """

    electricity_usd_per_kwh: Decimal
    efficiency_joules_per_terahash: Decimal
    reviewed_on: date
    review_every_days: int = Field(default=92, ge=1, le=366)
    # The lowest DMD/USD price until DMD has a market price: the price is the higher of this
    # reference rate and the mining cost. Unset: the mining cost alone.
    minimum_usd_per_dmd: Decimal | None = None
    # Date of DebitMyData's written approval of that minimum (shown with the price).
    minimum_approved_on: date | None = None

    @field_validator(
        "electricity_usd_per_kwh",
        "efficiency_joules_per_terahash",
        "minimum_usd_per_dmd",
        mode="before",
    )
    @classmethod
    def _quoted_positive(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError('must be a quoted decimal string, e.g. "0.06"')
        amount = parse_amount(value)
        if amount <= 0:
            raise ValueError("must be greater than zero")
        return amount

    @model_validator(mode="after")
    def _approval_needs_a_minimum(self) -> MiningCostParameters:
        if self.minimum_approved_on is not None and self.minimum_usd_per_dmd is None:
            raise ValueError("minimum_approved_on is set but minimum_usd_per_dmd is not")
        return self


class RegistryFile(_Strict):
    version: Literal[1]
    discovery_terms: tuple[str, ...] = ()
    services: dict[str, ServiceConfig]
    donations: dict[str, DonationConfig] = {}
    rails: dict[str, RailConfig]
    dmd_mining_cost: MiningCostParameters | None = None

    @model_validator(mode="after")
    def _cross_references(self) -> RegistryFile:
        for service_id, service in self.services.items():
            for rail_id in service.payment_options:
                if rail_id not in self.rails:
                    raise ValueError(f"service {service_id!r} references unknown rail {rail_id!r}")
        paths = [service.path for service in self.services.values()]
        for donation_id, donation in self.donations.items():
            if donation_id in self.services:
                raise ValueError(f"donation {donation_id!r} has the ID of a service")
            if donation.path in paths:
                raise ValueError(f"donation {donation_id!r} reuses the path {donation.path}")
            paths.append(donation.path)
            for rail_id in donation.payment_options:
                rail = self.rails.get(rail_id)
                if rail is None:
                    raise ValueError(
                        f"donation {donation_id!r} references unknown rail {rail_id!r}"
                    )
                if rail.protocol != "x402" or not rail.asset.usd_denominated:
                    raise ValueError(
                        f"donation {donation_id!r}: only x402 rails in a USD stablecoin can "
                        f"take donations ({rail_id!r} cannot)"
                    )
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
    if rail.protocol == "x402":
        problems.extend(x402_problems(rail, environ))
    return problems


def x402_problems(rail: RailConfig, environ: Mapping[str, str]) -> list[str]:
    """Extra conditions for an x402 rail (EVM "exact" scheme with EIP-3009 tokens)."""
    problems: list[str] = []
    if rail.scheme != "exact":
        problems.append(f"x402 scheme {rail.scheme!r} is not supported (only 'exact')")
    if (rail.transfer_method or "eip3009") not in X402_TRANSFER_METHODS:
        problems.append(f"x402 transfer method {rail.transfer_method!r} is not supported")
    if rail.evm_chain_id is None:
        problems.append(f"network {rail.network!r} is not an EVM network (eip155:<chain id>)")
    elif rail.asset.canonical_id and rail.token_contract is None:
        problems.append(f"asset.canonical_id must be {rail.network}/erc20:<0x contract address>")
    if rail.confirmation_policy.required_confirmations is None:
        problems.append("confirmation_policy.required_confirmations is not set")
    if rail.x402 is None:
        problems.append("x402 settings (token EIP-712 domain, facilitator, RPC) are missing")
    else:
        for name in (rail.x402.facilitator_url_env, rail.x402.rpc_url_env):
            if not (environ.get(name) or "").strip():
                problems.append(f"{name} is empty")
        problems.extend(_facilitator_key_problems(rail.x402, environ))
    recipient = (environ.get(rail.recipient.env or "") or "").strip()
    if recipient and not EVM_ADDRESS.match(recipient):
        problems.append(f"{rail.recipient.env} is not an EVM address (0x + 40 hex characters)")
    return problems


def _facilitator_key_problems(x402: X402Config, environ: Mapping[str, str]) -> list[str]:
    from dmd_gateway.adapters.cdp_auth import CdpApiKeyAuth  # here: adapters import this module

    url = (environ.get(x402.facilitator_url_env) or "").strip()
    host = url.split("://", 1)[-1].split("/", 1)[0].lower()
    credentials = facilitator_credentials(x402, environ)
    key_names = f"{x402.facilitator_key_id_env} and {x402.facilitator_key_secret_env}"
    if credentials is None:
        if host == CDP_FACILITATOR_HOST:
            return [f"the CDP facilitator needs an API key: set {key_names}"]
        return []
    if not (x402.facilitator_key_id_env and x402.facilitator_key_secret_env):
        return ["facilitator_key_id_env and facilitator_key_secret_env must both be configured"]
    key_id, secret = credentials
    if not key_id or not secret:
        return [f"set both {key_names}, or neither"]
    try:
        CdpApiKeyAuth(key_id, secret)
    except ValueError as exc:  # the message never contains the secret
        return [f"{x402.facilitator_key_secret_env}: {exc}"]
    return []


class PaymentRouteRegistry:
    def __init__(
        self,
        data: RegistryFile,
        states: Mapping[str, RailState],
        donation_recipients: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self._data = data
        self._states = dict(states)
        # (donation ID, rail ID) -> receiving address, for the configured ones only.
        self._donation_recipients = dict(donation_recipients or {})

    @property
    def services(self) -> Mapping[str, ServiceConfig]:
        return self._data.services

    @property
    def donations(self) -> Mapping[str, DonationConfig]:
        return self._data.donations

    def donation_rails(self, donation_id: str) -> list[tuple[str, RailConfig, str]]:
        """Enabled payment options of a donation that have a receiving address:
        (rail ID, rail, receiving address)."""
        donation = self._data.donations[donation_id]
        return [
            (rail_id, self._data.rails[rail_id], self._donation_recipients[(donation_id, rail_id)])
            for rail_id in donation.payment_options
            if self._states[rail_id] is RailState.ENABLED
            and (donation_id, rail_id) in self._donation_recipients
        ]

    @property
    def rails(self) -> Mapping[str, RailConfig]:
        return self._data.rails

    @property
    def discovery_terms(self) -> tuple[str, ...]:
        return self._data.discovery_terms

    @property
    def dmd_mining_cost(self) -> MiningCostParameters | None:
        return self._data.dmd_mining_cost

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
            mining_cost = "dmd_mining_cost"
            uses = {rail.price_policy.source, rail.price_policy.secondary_source}
            if mining_cost in uses and data.dmd_mining_cost is None:
                problems.append("dmd_mining_cost parameters are not set in the registry")
            if problems:
                raise RegistryError(
                    f"rail {rail_id!r} is switched on but not ready: " + "; ".join(problems)
                )
            states[rail_id] = RailState.ENABLED

    # A donation without a receiving address is simply not offered; a malformed address is a
    # configuration mistake and stops the gateway.
    donation_recipients: dict[tuple[str, str], str] = {}
    for donation_id, donation in data.donations.items():
        for rail_id, env in donation.recipients.items():
            address = (environ.get(env) or "").strip()
            if not address:
                continue
            if not EVM_ADDRESS.match(address):
                raise RegistryError(
                    f"donation {donation_id!r}: {env} is not an EVM address (0x + 40 hex)"
                )
            donation_recipients[(donation_id, rail_id)] = address
    return PaymentRouteRegistry(data, states, donation_recipients)
