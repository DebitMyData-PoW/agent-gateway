from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from dmd_gateway.main import create_app
from dmd_gateway.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "config" / "payment_routes.yaml"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# Nothing listens here, so readiness checks fail fast without a database.
UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:9/none"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL or UNREACHABLE_DB,
        database_connect_timeout_seconds=1,
        route_registry_path=REGISTRY_PATH,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1}, pool_pre_ping=True)
    app = create_app(settings, environ={}, engine=engine)
    with TestClient(app) as test_client:
        yield test_client


def alembic_config() -> Config:
    """Alembic configuration pointed at the disposable test database."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.cmd_opts = type("Opts", (), {"x": [f"url={TEST_DATABASE_URL}"]})()
    return config
