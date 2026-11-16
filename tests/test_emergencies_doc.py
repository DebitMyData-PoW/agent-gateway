"""The emergency runbook (docs/emergencies.md) names every payment option's kill switch exactly
as the route registry reads it, and no other: a renamed switch would not stop anything."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = (ROOT / "docs" / "emergencies.md").read_text(encoding="utf-8")


def test_every_kill_switch_is_in_the_runbook_as_the_registry_names_it() -> None:
    registry = yaml.safe_load((ROOT / "config" / "payment_routes.yaml").read_text())
    switches = {rail["kill_env"] for rail in registry["rails"].values()}
    named = set(re.findall(r"`(DMD_[A-Z0-9_]*KILL)=true`", RUNBOOK))
    assert named == switches


def test_the_runbook_points_to_what_exists() -> None:
    for script in re.findall(r"scripts/[a-z-]+\.sh", RUNBOOK):
        assert (ROOT / script).is_file(), script
    for anchor in re.findall(r"\]\(#([a-z0-9-]+)\)", RUNBOOK):
        headings = re.findall(r"^## (.+)$", RUNBOOK, flags=re.MULTILINE)
        slugs = {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in headings}
        assert anchor in slugs, anchor
