"""Environment-driven configuration (GW-02, GW-03).

Gateway settings use the ``DMD_GW_`` prefix. Payment-rail flags (``DMD_RAIL_*``) and
rail secrets such as merchant addresses are read by the route registry from
``runtime_environ()`` so that they come from the same place in every environment.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_DATABASE_URL = "postgresql+psycopg://gateway:gateway-dev-only@localhost:5432/agent_gateway"
ENV_FILE = ".env"
EMAIL = re.compile(r"^[^@\s,<>]+@[^@\s,<>]+\.[A-Za-z]{2,}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DMD_GW_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: str = DEV_DATABASE_URL
    database_connect_timeout_seconds: int = 3

    public_base_url: str = "http://localhost:8000"
    # The MCP endpoint is served at <public_base_url>/mcp. Set mcp_url only when agents reach
    # it at another public address (for example through a proxy).
    mcp_url: str | None = None
    # Extra Host header values accepted by the MCP endpoint (DNS-rebinding protection).
    mcp_allowed_hosts: list[str] = []

    route_registry_path: Path = Path("config/payment_routes.yaml")

    # How long a payment quote stays valid: 15 minutes, as in the directive's example (DEC-03).
    quote_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    # Open (unpaid, unexpired) quotes one controller key may have at a time. Stops a single
    # agent from piling up quotes, each with its own DMD address; per-address request rates
    # are limited in front of the gateway (nginx on staging).
    max_open_quotes_per_controller: int = Field(default=5, ge=1, le=100)

    # DMD/USD mining-cost price (pricing/mining_cost.py), computed by the payment worker every
    # dmd_price_refresh_seconds. Where the DMD chain figures (difficulty, height) come from:
    # "node" (the gateway's own DMD mainnet node, cross-checked against the explorer),
    # "explorer" (the public explorer, for servers without a mainnet node, such as staging),
    # or "off".
    dmd_price_chain_data_from: Literal["off", "node", "explorer"] = "off"
    dmd_price_refresh_seconds: int = Field(default=300, ge=60, le=3600)
    dmd_explorer_url: str = "https://explorer.debitmydata.io"

    # How often the payment worker checks open payments.
    worker_interval_seconds: float = Field(default=10, ge=1, le=600)

    # What signs DID credentials and donation receipts (ID-15). Development and staging: an
    # Ed25519 key file (PEM, mode 600). Production: the separate signer service
    # (dmd-gateway-signer) on its local socket, with the issuer's public key, which the API
    # checks every signature against. DID issuance is unavailable while neither is set.
    issuer_key_path: Path | None = None
    issuer_signer_socket: Path | None = None
    issuer_public_key: str | None = None

    # How long an agent has to sign a controller challenge.
    controller_challenge_ttl_seconds: int = Field(default=300, ge=60, le=900)

    # x402: how long a paid registration request waits for its settlement to reach the
    # required confirmations before answering 202 (the agent then simply asks again).
    x402_settlement_wait_seconds: float = Field(default=30, ge=0, le=120)

    # Staging only: test tools on the API docs page that stand in for an agent's own key and
    # wallet (a throwaway key, signing, paying a quote with test coins). Refused in
    # production. The DMD test payer uses DMD_RPC_URL/USER/PASSWORD with this wallet; the
    # x402 test payer signs with DMD_STAGING_BASE_PAYER_KEY (a Base Sepolia test wallet).
    staging_test_tools: bool = False
    staging_payer_wallet: str = Field(default="payer-test", min_length=1, max_length=64)

    # Monitoring and alerts (dmd-gateway-monitor, docs/monitoring.md). Email addresses are
    # comma-separated: alert_email_to receives every alert and its resolution,
    # alert_email_critical_to only critical alerts, report_email_to the daily report. The
    # SMTP password and the Discord webhook address are secrets, read from the environment
    # only (DMD_ALERT_SMTP_PASSWORD, DMD_ALERT_DISCORD_WEBHOOK_URL). These are plain strings,
    # checked by the monitor itself (monitoring/alerts.py): a mistake in them stops the
    # alerts, never the API or the payment worker.
    alert_email_to: str = ""
    alert_email_critical_to: str = ""
    report_email_to: str = ""
    alert_smtp_host: str = ""
    alert_smtp_port: str = "587"
    alert_smtp_security: str = "starttls"  # or ssl
    alert_smtp_username: str = ""
    alert_smtp_from: str = ""
    # Discord user mentioned in critical alerts, so they notify that person.
    alert_discord_mention_user_id: str = ""
    # An ongoing problem is sent again after this long, until it is resolved.
    alert_repeat_critical_minutes: int = Field(default=60, ge=5, le=1440)
    alert_repeat_warning_hours: int = Field(default=24, ge=1, le=168)
    # The API's readiness address as seen from the monitor (on the same server).
    monitor_api_url: str = "http://127.0.0.1:8000/readyz"
    # Where the monitor remembers alerts it sent while the database was unreachable.
    monitor_fallback_state_path: Path | None = None
    # Production: the file the nightly backup writes when it succeeds (its time, ISO 8601);
    # the monitor warns when it is older than a day and a bit.
    backup_status_path: Path | None = None

    @field_validator(
        "mcp_url",
        "issuer_key_path",
        "issuer_signer_socket",
        "issuer_public_key",
        "monitor_fallback_state_path",
        "backup_status_path",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("dmd_explorer_url")
    @classmethod
    def _https_explorer(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("dmd_explorer_url must be an https URL")
        return value.rstrip("/")

    @field_validator("public_base_url")
    @classmethod
    def _origin_only(cls, value: str) -> str:
        # Identities are did:web identifiers derived from this host, served from its root.
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("public_base_url must be an http(s) URL")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError(
                "public_base_url must not contain a path, e.g. https://api.example.com"
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def _guard_non_development(self) -> Settings:
        if self.environment != "development":
            if self.database_url == DEV_DATABASE_URL or "gateway-dev-only" in self.database_url:
                raise ValueError(
                    f"{self.environment} must not use the development database credentials"
                )
            if not self.public_base_url.startswith("https://"):
                raise ValueError(f"{self.environment} public_base_url must use https")
        if self.environment == "production" and self.issuer_key_path is not None:
            raise ValueError(
                "production must not keep the credential signing key on the API host (ID-15)"
            )
        if (self.issuer_signer_socket is None) != (self.issuer_public_key is None):
            raise ValueError(
                "issuer_signer_socket and issuer_public_key go together (the signer's socket "
                "and the public key it signs with)"
            )
        if self.issuer_signer_socket is not None and self.issuer_key_path is not None:
            raise ValueError("set issuer_key_path or issuer_signer_socket, not both")
        if self.issuer_public_key is not None and not re.fullmatch(
            r"z6Mk[1-9A-HJ-NP-Za-km-z]{40,50}", self.issuer_public_key
        ):
            raise ValueError("issuer_public_key must be an Ed25519 Multikey (z6Mk...)")
        if self.environment == "production" and self.staging_test_tools:
            raise ValueError("staging test tools must never be enabled in production")
        return self


def email_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def runtime_environ(env_file: str | Path = ENV_FILE) -> Mapping[str, str]:
    """Process environment layered over the optional .env file (process env wins)."""
    file_values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    return {**file_values, **os.environ}
