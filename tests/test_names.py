"""Display names: hidden characters, mixed alphabets and lookalikes of DebitMyData's own names
are refused when the quote is requested (identity/names.py)."""

from __future__ import annotations

import pytest

from dmd_gateway.identity.names import (
    NameRefused,
    brand_owners,
    check_display_name,
    is_brand_owner,
    skeleton,
)

from .fakes import AGENT_CONTROLLER


@pytest.mark.parametrize(
    "name",
    [
        "Staging test agent",
        "TestAgent-01",
        "ResearchClaw-01",
        "Café Bot",
        "Straße-Agent",
        "Агент Исследователь",  # all Cyrillic
        "東京タワー案内",  # Japanese: kanji with katakana and the long-vowel mark
        "日本語　エージェント",  # with the ideographic space
        "한국어 도우미",
        "وكيل البحث",
        "خرید‌یار",  # Persian needs the zero-width non-joiner
        "अनुसंधान एजेंट",
        "Bot 👨‍💻",  # an emoji sequence joined with ZWJ
        "DMDirect",  # contains DMD but is another word
        "Debit Card Helper",
        "Data Bot",
    ],
)
def test_ordinary_names_pass(name: str) -> None:
    check_display_name(name)


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("Agent​X", "hidden character (U+200B ZERO WIDTH SPACE)"),
        ("Agent‮X", "hidden character (U+202E RIGHT-TO-LEFT OVERRIDE)"),
        ("Agent‍X", "hidden character"),  # a joiner between Latin letters
        ("Agent X", "unusual space (U+00A0 NO-BREAK SPACE)"),
        ("Za̵̡̢͍̲lgo", "too many accents"),
        ("DеbitMyData", "mixes letters from different alphabets (Cyrillic and Latin)"),
        ("Pаypal", "mixes letters"),
        ("DebitMyData", "DebitMyData's own names"),
        ("DebitMyData TEST 1", "DebitMyData's own names"),
        ("Debit My Data assistant", "DebitMyData's own names"),
        ("debit-my-data", "DebitMyData's own names"),
        ("D3bitMyD4ta", "DebitMyData's own names"),  # digits for letters
        ("DebitRnyData", "DebitMyData's own names"),  # rn for m
        ("ＤｅｂｉｔＭｙＤａｔａ", "DebitMyData's own names"),  # full-width letters
        ("DMD", "DebitMyData's own names"),
        ("DMD Official", "DebitMyData's own names"),
        ("D.M.D.", "DebitMyData's own names"),
        ("clmcl", "DebitMyData's own names"),  # cl for d
        ("Botty Botty Pro", "DebitMyData's own names"),
        ("ВОТТУ ВОТТУ", "DebitMyData's own names"),  # BOTTY in Cyrillic
        ("Agentic Spaces helper", "DebitMyData's own names"),
    ],
)
def test_lookalike_and_hidden_names_are_refused(name: str, words: str) -> None:
    with pytest.raises(NameRefused, match=None) as refused:
        check_display_name(name)
    assert words in str(refused.value)


def test_the_company_may_use_its_names_but_not_lookalike_letters() -> None:
    check_display_name("DebitMyData TEST 1", brand_owner=True)
    check_display_name("DMD treasury agent", brand_owner=True)
    with pytest.raises(NameRefused, match="mixes letters"):
        check_display_name("DеbitMyData", brand_owner=True)
    with pytest.raises(NameRefused, match="hidden character"):
        check_display_name("Debit​MyData", brand_owner=True)


def test_skeleton_sees_through_lookalikes() -> None:
    assert skeleton("Dеbit My-Data!") == skeleton("debitmydata") == "debitmydata"
    assert skeleton("B0TTY b0tty") == "bottybotty"


def test_brand_owners_from_the_settings() -> None:
    owners = brand_owners(
        f" {AGENT_CONTROLLER}, eip155:8453:0x52908400098527886E0F7030069857D2E4169eE7 "
    )
    assert AGENT_CONTROLLER in owners
    assert "0x52908400098527886e0f7030069857d2e4169ee7" in owners
    assert is_brand_owner(owners, AGENT_CONTROLLER, None)
    assert is_brand_owner(owners, None, "0x52908400098527886E0F7030069857D2E4169eE7")
    assert not is_brand_owner(owners, None, "0x" + "11" * 20)
    assert brand_owners("") == frozenset()
    with pytest.raises(ValueError, match="DMD_GW_BRAND_NAME_CONTROLLERS"):
        brand_owners("not-a-key")
