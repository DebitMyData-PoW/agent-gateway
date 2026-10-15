import re

import pytest

from dmd_gateway.domain.ids import REGISTRATION_INTENT, new_id

FORMAT = re.compile(r"^dmd_did_intent_[0-9A-HJKMNP-TV-Z]{26}$")


def test_format() -> None:
    assert FORMAT.match(new_id(REGISTRATION_INTENT))


def test_unique_and_time_sortable() -> None:
    ids = [new_id(REGISTRATION_INTENT) for _ in range(5000)]
    assert len(set(ids)) == len(ids)
    timestamps = [i.rsplit("_", 1)[1][:10] for i in ids]
    assert timestamps == sorted(timestamps)


@pytest.mark.parametrize("prefix", ["", "Upper", "has space", "x" * 50, "1abc"])
def test_bad_prefix(prefix: str) -> None:
    with pytest.raises(ValueError):
        new_id(prefix)
