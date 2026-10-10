"""Limit prices for limit-on-open buys (ADR-0027). Pure, exact (Decimal), no network.

Alpaca (orders-at-alpaca, verified 2026-10-10): limit prices >= 1.00 accept at most 2 decimals,
below 1.00 at most 4; anything finer is rejected ("sub-penny increment does not fulfill minimum
pricing criteria"). A buy limit order never fills above its limit price, so with
`limit = floor_to_increment(budget / shares)` the value of `shares` at any fill price is
<= shares x limit <= budget.
"""

from decimal import ROUND_FLOOR, Decimal

CENT = Decimal("0.01")
SUB_DOLLAR_TICK = Decimal("0.0001")


def _tick(price: Decimal) -> Decimal:
    return CENT if price >= 1 else SUB_DOLLAR_TICK


def budget_limit_price(budget: float, shares: float) -> Decimal:
    """Highest valid limit price p with shares x p <= budget (rounded DOWN to the increment)."""
    b, n = Decimal(str(budget)), Decimal(str(shares))
    if b <= 0 or n <= 0:
        raise ValueError("budget and shares must be > 0")
    raw = b / n
    p = raw.quantize(CENT, rounding=ROUND_FLOOR)
    if p < 1:
        p = raw.quantize(SUB_DOLLAR_TICK, rounding=ROUND_FLOOR)
    if p <= 0:
        raise ValueError("budget too small for one share at the minimum increment")
    if n * p > b:  # cannot happen with ROUND_FLOOR; kept as an explicit guarantee
        raise ArithmeticError("limit price exceeds the budget")
    return p


def format_limit_price(price: float | Decimal) -> str:
    """Exact wire format; refuses a price that is not on Alpaca's increment (no silent rounding)."""
    d = Decimal(str(price))
    if d <= 0:
        raise ValueError("limit price must be > 0")
    tick = _tick(d)
    if d != d.quantize(tick):
        raise ValueError(f"limit price {d} is not on the {tick} increment")
    return str(d.quantize(tick))
