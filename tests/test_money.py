from decimal import Decimal

import pytest

from dmd_gateway.domain.money import (
    AmountError,
    format_base_units,
    from_base_units,
    parse_amount,
    to_base_units,
    usd_to_base_units,
)


def test_float_is_refused() -> None:
    with pytest.raises(AmountError):
        parse_amount(25.0)  # type: ignore[arg-type]
    with pytest.raises(AmountError):
        to_base_units(0.1, 8)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity", "abc", ""])
def test_invalid_amounts_are_refused(bad: str) -> None:
    with pytest.raises(AmountError):
        parse_amount(bad)


def test_round_trip_dmd() -> None:
    assert to_base_units("125.5", 8) == 12_550_000_000
    assert from_base_units(12_550_000_000, 8) == Decimal("125.5")
    assert format_base_units(12_550_000_000, 8) == "125.50000000"


def test_usdc_six_decimals() -> None:
    assert to_base_units("25.00", 6) == 25_000_000
    assert format_base_units(25_000_000, 6) == "25.000000"


def test_sub_base_unit_precision_is_refused() -> None:
    with pytest.raises(AmountError):
        to_base_units("0.000000001", 8)


def test_usd_conversion_rounds_up_never_down() -> None:
    # 25 USD at 0.20 USD per DMD -> exactly 125 DMD
    assert usd_to_base_units("25.00", "0.2", 8) == 12_500_000_000
    # 25 / 3 = 8.333... -> rounded UP to the next base unit
    assert usd_to_base_units("25.00", "3", 8) == 833_333_334
    assert usd_to_base_units("25.00", "1", 6) == 25_000_000


def test_zero_price_is_refused() -> None:
    with pytest.raises(AmountError):
        usd_to_base_units("25.00", "0", 8)


def test_eighteen_decimal_tokens_are_exact() -> None:
    units = to_base_units("123456789.123456789123456789", 18)
    assert units == 123_456_789_123_456_789_123_456_789
    assert from_base_units(units, 18) == Decimal("123456789.123456789123456789")
