"""Globally unique, time-sortable identifiers with a readable prefix (QUOTE-06).

Format: ``<prefix>_<26 Crockford base32 chars>`` (48-bit millisecond timestamp + 80 random
bits), e.g. ``dmd_did_intent_01JA8Z3T6H5K9V2M4Q7R8S0W1X``.
"""

from __future__ import annotations

import re
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PREFIX = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

REGISTRATION_INTENT = "dmd_did_intent"
PAYMENT_INTENT = "dmd_payment_intent"
IDENTITY = "dmd_identity"
CHALLENGE = "dmd_challenge"
PROOF = "dmd_proof"
CREDENTIAL = "dmd_credential"
PURCHASE = "dmd_purchase"

REGISTRATION_INTENT_ID_PATTERN = rf"^{REGISTRATION_INTENT}_[0-9A-HJKMNP-TV-Z]{{26}}$"

# Reserved example registration ID, shown in the x402 Bazaar listing. Never a real quote:
# generated IDs start with the current time, this one with 1970.
EXAMPLE_REGISTRATION_INTENT_ID = f"{REGISTRATION_INTENT}_{'0' * 26}"


def new_id(prefix: str) -> str:
    if not _PREFIX.match(prefix):
        raise ValueError(f"invalid id prefix: {prefix!r}")
    timestamp_ms = (time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    value = (timestamp_ms << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"
