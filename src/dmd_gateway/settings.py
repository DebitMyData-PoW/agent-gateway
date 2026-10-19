"""Environment-driven configuration (GW-02, GW-03).

Gateway settings use the ``DMD_GW_`` prefix. Payment-rail flags (``DMD_RAIL_*``) and
rail secrets such as merchant addresses are read by the route registry from
``runtime_environ()`` so that they come from the same place in every environment.
"""

from __future__ import annotations

import os
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

    # How often the payment worker checks open payments.
    worker_interval_seconds: float = Field(default=10, ge=1, le=600)

    # Ed25519 key that signs registration credentials (PEM file, mode 600). DID issuance is
    # unavailable while this is unset. Production needs a separate signer instead (ID-15).
    issuer_key_path: Path | None = None

    # How long an agent has to sign a controller challenge.
    controller_challenge_ttl_seconds: int = Field(default=300, ge=60, le=900)

    @field_validator("mcp_url", "issuer_key_path", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def runtime_environ(env_file: str | Path = ENV_FILE) -> Mapping[str, str]:
    """Process environment layered over the optional .env file (process env wins)."""
    file_values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    return {**file_values, **os.environ}
