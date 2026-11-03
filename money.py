"""Money handling: exact decimals at the edges, whole microdollars inside.

All spending arithmetic and comparisons use integer microdollars (1 USD = 1,000,000), so
budget ceilings are enforced exactly, with no floating-point rounding or tolerance.
Settings and admin input are parsed as Decimal and must be exactly representable in
microdollars: a value with more than 6 decimal places is rejected, never rounded.
"""
from decimal import Decimal, InvalidOperation

MICROS_PER_USD = 1_000_000


def usd_to_micros(value) -> int:
    """Exact conversion of a USD amount (str, int or Decimal) to whole microdollars.

    Raises ValueError for non-numbers, non-finite values, and amounts with more than 6
    decimal places. Floats are refused: they cannot be trusted to hold a price exactly.
    """
    if isinstance(value, (float, bool)):
        raise ValueError("Money must be given as text, a whole number or a Decimal, not a float.")
    try:
        amount = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        raise ValueError(f"{value!r} is not a number.") from None
    if not amount.is_finite():
        raise ValueError(f"{value!r} is not a finite number.")
    micros = amount * MICROS_PER_USD
    if micros != micros.to_integral_value():
        raise ValueError(f"{value!r} has more than 6 decimal places; the smallest unit is $0.000001.")
    return int(micros)


def micros_to_usd(micros: int) -> Decimal:
    return Decimal(micros) / MICROS_PER_USD


def format_usd(micros: int, places: int = 4) -> str:
    """For display only, e.g. 900000 -> '$0.9000'."""
    sign = "-" if micros < 0 else ""
    return f"{sign}${abs(micros_to_usd(micros)):.{places}f}"


def require_micros(value, name="amount") -> int:
    """Guard for the usage database: money must already be whole microdollars (an int)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number of microdollars, got {value!r}.")
    return value
