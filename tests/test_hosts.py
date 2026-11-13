"""One address for people (the app's, id.debitmydata.io) and one for agents and developers
(the API's): the pages lead to theirs, the API and what agents read stay at both."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from dmd_gateway.main import create_app
from dmd_gateway.settings import Settings
from tests.conftest import UNREACHABLE_DB

APP = "id.debitmydata.io"
API = "http://localhost:8000"  # the tests' public_base_url


@pytest.fixture
def gateway(settings: Settings) -> Iterator[TestClient]:
    """The production gateway, answering at both names like behind nginx."""
    engine = create_engine(UNREACHABLE_DB, connect_args={"connect_timeout": 1})
    app = create_app(settings.model_copy(update={"app_host": APP}), environ={}, engine=engine)
    with TestClient(app, base_url=API, follow_redirects=False) as test_client:
        yield test_client


def at_app(client: TestClient, path: str) -> object:
    return client.get(path, headers={"Host": APP})


@pytest.mark.parametrize(
    ("path", "there"),
    [
        ("/get-did", "/"),  # the app serves "Get a DID-LLM" at its root
        ("/get-did?card=back&payment=pi_1", "/?card=back&payment=pi_1"),
        ("/", "/"),
        ("/policies", "/policies"),
        ("/policies/refunds", "/policies/refunds"),
        ("/directory?q=robot", "/directory?q=robot"),
    ],
)
def test_pages_for_people_lead_from_the_apis_address_to_the_apps(
    gateway: TestClient, path: str, there: str
) -> None:
    moved = gateway.get(path)
    assert moved.status_code == 308  # permanent, and keeps the method
    assert moved.headers["location"] == f"https://{APP}{there}"


def test_the_app_serves_them_itself(gateway: TestClient) -> None:
    for path in ("/", "/policies", "/policies/refunds"):
        response = at_app(gateway, path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", ["/docs/advanced", "/redoc"])
def test_the_api_docs_lead_from_the_apps_address_to_the_apis(
    gateway: TestClient, path: str
) -> None:
    moved = at_app(gateway, path)
    assert moved.status_code == 308
    assert moved.headers["location"] == f"{API}{path}"
    assert gateway.get(path).status_code == 200  # at the API's address, the docs themselves


@pytest.mark.parametrize(
    "path",
    ["/policies/refunds.md", "/v1/policies", "/v1/discovery", "/llms.txt", "/openapi.json"],
)
def test_what_agents_read_answers_at_both_addresses(gateway: TestClient, path: str) -> None:
    assert gateway.get(path).status_code == 200
    assert at_app(gateway, path).status_code == 200


def test_without_an_app_host_nothing_moves(client: TestClient) -> None:
    for path in ("/get-did", "/policies", "/policies/refunds", "/docs/advanced", "/redoc"):
        assert client.get(path, follow_redirects=False).status_code == 200, path
