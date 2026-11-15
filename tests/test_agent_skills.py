"""The Agent Skills index (api/agent_skills.py): the skill the gateway writes itself, listed at
/.well-known/agent-skills/index.json with the SHA-256 digest of the very text it serves."""

from __future__ import annotations

import hashlib
import re

import yaml
from fastapi.testclient import TestClient

from dmd_gateway.api import agent_skills

from .test_get_did_page import APP_HOST, app_client  # noqa: F401

API = {"host": "localhost:8000"}
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def test_the_index_lists_the_skill_with_its_digest_at_both_names(
    app_client: TestClient,  # noqa: F811
) -> None:
    for headers in ({}, API):
        response = app_client.get(agent_skills.INDEX_PATH, headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["access-control-allow-origin"] == "*"
        index = response.json()
        assert index["$schema"] == "https://schemas.agentskills.io/discovery/0.2.0/schema.json"
        (skill,) = index["skills"]
        assert set(skill) == {"name", "type", "description", "url", "digest"}
        assert skill["name"] == "debitmydata-did-llm" and NAME.match(skill["name"])
        assert len(skill["name"]) <= 64 and len(skill["description"]) <= 1024
        assert skill["type"] == "skill-md"
        assert skill["url"] == "/.well-known/agent-skills/debitmydata-did-llm/SKILL.md"
        served = app_client.get(skill["url"], headers=headers)
        assert served.status_code == 200
        assert served.headers["content-type"] == "text/markdown; charset=utf-8"
        assert skill["digest"] == f"sha256:{hashlib.sha256(served.content).hexdigest()}"
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", skill["digest"])


def test_the_skill_is_the_gateways_own_text(app_client: TestClient) -> None:  # noqa: F811
    index = app_client.get(agent_skills.INDEX_PATH).json()
    text = app_client.get(agent_skills.SKILL_PATH).text
    # Same skill, same words at both names: the digest holds wherever it is read.
    assert app_client.get(agent_skills.SKILL_PATH, headers=API).text == text
    _, frontmatter, body = text.split("---\n", 2)
    meta = yaml.safe_load(frontmatter)
    assert meta == {"name": "debitmydata-did-llm", "description": index["skills"][0]["description"]}
    assert "$25.00 USD, paid once" in meta["description"]
    assert body.lstrip().startswith("# DebitMyData DID-LLM\n\nUse this skill when you")
    assert "`dmd_open_checkout`" in body and "`dmd_verify_agent`" in body
    assert "http://localhost:8000/agenticsuperhighway" in body
    assert agent_skills.SHARED_SKILL in body
    # llms.txt, below the skill's own title: its headings one level down.
    llms = app_client.get("/llms.txt", headers=API).text
    assert "\n## DebitMyData DID-LLM\n" in body and "\n### With MCP\n" in body
    assert "It costs $25.00 USD, paid once." in llms and "It costs $25.00 USD, paid once." in body
    assert "\n# " not in body.split("# DebitMyData DID-LLM", 1)[1]  # one title only
