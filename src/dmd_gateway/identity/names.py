"""Display names that cannot pass for another one.

A DID's display name is public (it is in the DID's credential), so it is checked when the
quote is requested, before anything is paid:

1. No hidden characters: no zero-width or text-direction marks, no control characters, no
   space other than the ordinary one, and no pile of accents on one letter.
2. One alphabet per word: a word that mixes alphabets, such as "DebitMyData" written with a
   Cyrillic "е", is how lookalike names are made. Words written entirely in another alphabet
   are fine, and so is Japanese (kanji with kana) or Korean (hangul with hanja).
3. DebitMyData's own names are reserved: a name that looks like DebitMyData, BottyBotty,
   AgenticSpaces or DMD once capitals, spaces, punctuation, accents and lookalike letters and
   digits are set aside, is refused unless its controller is one of the company's, listed in
   the server settings (``DMD_GW_BRAND_NAME_CONTROLLERS``).

The lookalike table is deliberately small: letters that look the same as Latin ones in the
Cyrillic, Greek and Armenian alphabets, and the digits and letter pairs used in their place.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from dmd_gateway.identity.crypto import ControllerKeyError, parse_controller_key

# Compared inside the whole name, without spaces: "Debit My Data Bot" is refused.
RESERVED = (
    "debitmydata",
    "bottybotty",
    "agenticspaces",
    "agenticsocialspaces",
    "agenticssocialspaces",
)
# Compared as a whole word only: "DMD" and "D.M.D. bot" are refused, "DMDirect" is not.
RESERVED_WORDS = ("dmd",)
CONTACT = "info@debitmydata.com"

_HIDDEN = {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"}
_MARKS = {"Mn", "Me"}
_JOINERS = {"\u200c", "\u200d"}  # needed in Persian, Indic scripts and emoji
_SPACES = {" ", "\u3000"}  # the ordinary space and the ideographic (Japanese) one
_MAX_MARKS = 4  # accents on one letter
_LOOKALIKE_SCRIPTS = {"Latin", "Greek", "Cyrillic", "Armenian"}
_ALLOWED_MIXES = (
    frozenset({"Han", "Hiragana", "Katakana"}),
    frozenset({"Han", "Hangul"}),
    frozenset({"Han", "Bopomofo"}),
)
_SCRIPT_WORDS = {
    "CJK": "Han",
    "HIRAGANA": "Hiragana",
    "KATAKANA": "Katakana",
    "HANGUL": "Hangul",
    "BOPOMOFO": "Bopomofo",
}
# Letters that look like Latin ones (lower case, after case folding).
_LOOKALIKES = {
    # Cyrillic
    "а": "a",
    "в": "b",
    "е": "e",
    "ё": "e",
    "з": "3",
    "і": "i",
    "ї": "i",
    "ј": "j",
    "к": "k",
    "м": "m",
    "н": "h",
    "о": "o",
    "р": "p",
    "с": "c",
    "т": "t",
    "у": "y",
    "х": "x",
    "ѕ": "s",
    "һ": "h",
    "ԁ": "d",
    "ԛ": "q",
    "ԝ": "w",
    "ь": "b",
    "ӏ": "l",
    "п": "n",
    "г": "r",
    # Greek
    "α": "a",
    "β": "b",
    "ε": "e",
    "η": "n",
    "ι": "i",
    "κ": "k",
    "μ": "m",
    "ν": "v",
    "ο": "o",
    "ρ": "p",
    "τ": "t",
    "υ": "u",
    "χ": "x",
    "γ": "y",
    "ζ": "z",
    "ω": "w",
    "σ": "o",
    "ς": "c",
    # Armenian
    "օ": "o",
    "ս": "u",
    "ց": "g",
    "հ": "h",
    "ո": "n",
    "զ": "q",
    "ա": "w",
    # Latin letters that stand for others
    "ı": "i",
    "ł": "l",
    "ø": "o",
    "đ": "d",
    "ð": "d",
    "ħ": "h",
    "ƅ": "b",
    "ɡ": "g",
}
# Then digits and letters that are read the same, so that both sides compare equal.
_SAME_LOOK = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "l": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "9": "g",
        "6": "b",
    }
)
_PAIRS = (("rn", "m"), ("cl", "d"), ("vv", "w"))


class NameRefused(ValueError):
    """The display name is refused; the message says why, for the person who chose it."""


def check_display_name(name: str, *, brand_owner: bool = False) -> None:
    """Raise NameRefused if the name hides characters, mixes alphabets in a word, or looks
    like one of DebitMyData's own names (unless ``brand_owner``)."""
    _no_hidden_characters(name)
    _one_alphabet_per_word(name)
    if not brand_owner:
        _not_reserved(name)


def skeleton(text: str) -> str:
    """The text as it looks: lower case, no accents, lookalike letters and digits replaced,
    only a-z and 0-9 kept (spaces and punctuation dropped)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = "".join(_LOOKALIKES.get(ch, ch) for ch in folded)
    folded = "".join(
        ch for ch in unicodedata.normalize("NFD", folded) if unicodedata.category(ch) not in _MARKS
    )
    kept = re.sub(r"[^a-z0-9]", "", folded)
    for pair, single in _PAIRS:
        kept = kept.replace(pair, single)
    return kept.translate(_SAME_LOOK)


def brand_owners(values: str | Iterable[str]) -> frozenset[str]:
    """The company's controllers from the settings (comma or space separated): agent keys
    (ed25519:...) in their canonical form, wallets as lower-case 0x addresses."""
    items = values.replace(",", " ").split() if isinstance(values, str) else list(values)
    owners = set()
    for item in items:
        if item.lower().startswith("0x") or item.startswith("eip155:"):
            owners.add(item.rsplit(":", 1)[-1].lower())
            continue
        try:
            owners.add(parse_controller_key(item).canonical)
        except ControllerKeyError as exc:
            raise ValueError(
                f"DMD_GW_BRAND_NAME_CONTROLLERS: {item!r} is not a key or wallet"
            ) from exc
    return frozenset(owners)


def is_brand_owner(owners: frozenset[str], public_key: str | None, wallet: str | None) -> bool:
    if public_key and public_key in owners:
        return True
    return bool(wallet) and wallet.rsplit(":", 1)[-1].lower() in owners


# --------------------------------------------------------------------------- the checks


def _no_hidden_characters(name: str) -> None:
    marks = 0
    for index, ch in enumerate(name):
        category = unicodedata.category(ch)
        if ch in _JOINERS and _joiner_in_place(name, index):
            continue
        if category in _HIDDEN:
            raise NameRefused(
                f"The name contains a hidden character ({_describe(ch)}), such as a zero-width "
                "or text-direction mark. Type the name again without it."
            )
        if category == "Zs" and ch not in _SPACES:
            raise NameRefused(
                f"The name contains an unusual space ({_describe(ch)}): use ordinary spaces."
            )
        marks = marks + 1 if category in _MARKS else 0
        if marks > _MAX_MARKS:
            raise NameRefused("The name has too many accents on one letter.")


def _joiner_in_place(name: str, index: int) -> bool:
    """A zero-width joiner or non-joiner between two characters of a script that needs it
    (or of an emoji), never next to a space or a letter that has Latin lookalikes."""
    if index == 0 or index == len(name) - 1:
        return False
    for ch in (name[index - 1], name[index + 1]):
        if ch.isspace() or ch in _JOINERS:
            return False
        if unicodedata.category(ch).startswith("L") and _script(ch) in _LOOKALIKE_SCRIPTS:
            return False
    return True


def _one_alphabet_per_word(name: str) -> None:
    for word in _words(name):
        scripts = {_script(ch) for ch in word if unicodedata.category(ch).startswith("L")}
        scripts.discard(None)
        if len(scripts) > 1 and not any(scripts <= mix for mix in _ALLOWED_MIXES):
            alphabets = " and ".join(sorted(s.title() for s in scripts if s))
            raise NameRefused(
                f"The word {word!r} mixes letters from different alphabets ({alphabets}), which "
                "is how lookalike names are made. Write each word in one alphabet."
            )


def _not_reserved(name: str) -> None:
    whole = skeleton(name)
    for reserved in RESERVED:
        if skeleton(reserved) in whole:
            _reserved(name)
    for word in _words(name):
        if skeleton(word) in RESERVED_WORDS:
            _reserved(name)
    # "D.M.D." and "D M D": the letters of a reserved word written apart.
    if any(whole == skeleton(word) for word in RESERVED_WORDS):
        _reserved(name)


def _reserved(name: str) -> None:
    raise NameRefused(
        f"{name!r} looks like one of DebitMyData's own names, which are reserved for its own "
        f"DID-LLMs. Choose another name, or write to {CONTACT} to register a DID-LLM for "
        "DebitMyData."
    )


def _words(name: str) -> list[str]:
    words, current = [], ""
    for ch in name:
        category = unicodedata.category(ch)
        if category[0] in "LMN":
            current += ch
        elif current:
            words.append(current)
            current = ""
    if current:
        words.append(current)
    return words


def _script(ch: str) -> str | None:
    """The alphabet of a letter, from its Unicode name (LATIN ..., CYRILLIC ..., CJK ...)."""
    normal = unicodedata.normalize("NFKC", ch)[:1] or ch
    try:
        first = unicodedata.name(normal).split(" ", 1)[0]
    except ValueError:
        return None
    if first in ("MODIFIER", "SMALL", "IDEOGRAPHIC", "KATAKANA-HIRAGANA"):
        return None
    return _SCRIPT_WORDS.get(first, first.title())


def _describe(ch: str) -> str:
    try:
        return f"U+{ord(ch):04X} {unicodedata.name(ch)}"
    except ValueError:
        return f"U+{ord(ch):04X}"
