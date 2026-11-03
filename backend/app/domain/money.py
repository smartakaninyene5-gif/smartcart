"""Decimal money helpers. Floats are never accepted as money."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def currency_code(value: str, field: str = "currency") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"{field} must be a 3-letter ISO-4217 code, got {value!r}")
    return code


def money(
    value: object,
    field: str = "amount",
    *,
    allow_zero: bool = True,
    allow_negative: bool = False,
) -> Decimal:
    """Parse a value into an exact 2-decimal Decimal.

    Bools are rejected (``bool`` is an ``int`` in Python and would silently
    become 1.00). Values with more than 2 decimal places are rejected rather
    than rounded, so a provider cannot smuggle a fraction of a cent past us.
    """

    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number, not a boolean")
    if isinstance(value, float):
        # repr() avoids binary float artefacts while still being exact.
        value = repr(value)
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not a valid decimal: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"{field} must be finite")
    if amount != amount.quantize(CENT):
        raise ValueError(f"{field} must have at most 2 decimal places")
    if not allow_negative and amount < 0:
        raise ValueError(f"{field} must not be negative")
    if not allow_zero and amount == 0:
        raise ValueError(f"{field} must be greater than zero")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def money_sum(values: tuple[Decimal, ...], currency: str) -> Decimal:
    total = sum(values, ZERO)
    return money(total, f"total ({currency_code(currency)})")


def format_money(amount: Decimal, currency: str) -> str:
    return f"{money(amount):.2f} {currency_code(currency)}"
