"""Exact money and token arithmetic (QUOTE-13).

Rules:
- Binary floating point is rejected everywhere; amounts are ``Decimal`` or decimal strings.
- Token amounts are stored and compared as integer base units (e.g. 1 DMD = 10^8 base units).
- When converting a USD price into a token amount, round UP to the next base unit so the
  quoted payment never settles for less than the commercial price.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext

MAX_DECIMALS = 36


class AmountError(ValueError):
    pass


def parse_amount(value: str | int | Decimal) -> Decimal:
    """Parse a non-negative amount. Floats and non-finite values are refused."""
    if isinstance(value, bool) or isinstance(value, float):
        raise AmountError("amounts must be Decimal, int or decimal strings, never float")
    try:
        amount = Decimal(value) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError) as exc:
        raise AmountError(f"invalid amount: {value!r}") from exc
    if not amount.is_finite():
        raise AmountError("amount must be finite")
    if amount < 0:
        raise AmountError("amount must not be negative")
    return amount


def _check_decimals(decimals: int) -> None:
    if not 0 <= decimals <= MAX_DECIMALS:
        raise AmountError(f"decimals must be between 0 and {MAX_DECIMALS}")


def to_base_units(amount: str | int | Decimal, decimals: int) -> int:
    """Convert a display amount to integer base units. Refuses sub-base-unit precision."""
    _check_decimals(decimals)
    value = parse_amount(amount)
    with localcontext() as ctx:
        ctx.prec = 100
        scaled = value.scaleb(decimals)
        if scaled != scaled.to_integral_value():
            raise AmountError(f"{value} has more precision than {decimals} decimals")
        return int(scaled)


def from_base_units(units: int, decimals: int) -> Decimal:
    _check_decimals(decimals)
    if isinstance(units, bool) or not isinstance(units, int):
        raise AmountError("base units must be an int")
    if units < 0:
        raise AmountError("base units must not be negative")
    with localcontext() as ctx:
        ctx.prec = 100
        return Decimal(units).scaleb(-decimals)


def format_base_units(units: int, decimals: int) -> str:
    """Fixed-point string with exactly ``decimals`` places (12550000000, 8 -> '125.50000000')."""
    value = from_base_units(units, decimals)
    return f"{value:.{decimals}f}"


def usd_to_base_units(usd_amount: str | Decimal, usd_per_unit: str | Decimal, decimals: int) -> int:
    """Base units of an asset needed to cover ``usd_amount`` at ``usd_per_unit``, rounded up."""
    _check_decimals(decimals)
    usd = parse_amount(usd_amount)
    price = parse_amount(usd_per_unit)
    if price == 0:
        raise AmountError("price must be greater than zero")
    with localcontext() as ctx:
        ctx.prec = 100
        units = (usd / price).scaleb(decimals).to_integral_value(rounding=ROUND_CEILING)
        return int(units)
