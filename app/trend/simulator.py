"""Causal replay with fills at the next close, EUR costs, cash interest and accounting
(design v2 sections 6.4, 6.5 and 8.4).

Order of events on each master day ``T`` of the run:

1. cash interest from the previous master day (rate dated on or before it);
2. marks: assets with a session on ``T`` move to their close (price P&L on units held);
3. fills: orders decided at an earlier ``D`` whose asset has a session on ``T`` fill at that
   close, sells before buys; buys are cut (whole units) so cash never goes negative;
4. NAV and the accounting identity;
5. decision ``D(T)``: the policy sees closes up to ``T`` only and returns target weights for
   the assets to trade; quantities come from these prices (whole units, toward zero); orders
   for assets with a pending order are never created.

A decision without a later close stays pending and is never filled synthetically.
"""

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd

from app.trend.allocator import whole_units
from app.trend.costs import CostModel
from app.trend.data import interest

TOLERANCE = 1e-7  # EUR, accounting identity per day


class AccountingError(AssertionError):
    """An accounting identity of section 8.4 does not hold."""


@dataclass(frozen=True)
class Context:
    day: pd.Timestamp
    prev_day: pd.Timestamp | None
    first: bool
    nav: float
    units: dict[str, int]
    prices: dict[str, float]  # latest close at or before D(day), per asset
    pending: frozenset[str]


class Policy(Protocol):
    @property
    def assets(self) -> tuple[str, ...]: ...

    def decide(self, ctx: Context) -> dict[str, float]:
        """Target weights for the assets to trade at D(ctx.day)."""
        ...


@dataclass
class Order:
    asset: str
    quantity: int  # signed; positive = buy
    decided: pd.Timestamp


@dataclass
class RunResult:
    daily: pd.DataFrame
    trades: list[dict[str, Any]]
    asset_pnl: dict[str, dict[str, float]]
    flags: list[str] = field(default_factory=list)
    pending_at_end: list[dict[str, Any]] = field(default_factory=list)


def _price_on(series: pd.Series, day: pd.Timestamp) -> float | None:
    """Close on exactly `day`, or None when the asset has no session that day."""
    value = series.get(day)
    return None if value is None else float(value)


def _latest(series: pd.Series, day: pd.Timestamp) -> float | None:
    upto = series.loc[series.index <= day]
    return None if upto.empty else float(upto.iloc[-1])


def simulate(
    prices: dict[str, pd.Series],
    master: pd.DatetimeIndex,
    cash_rate: pd.Series,
    costs: CostModel,
    policy: Policy,
    capital: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    flags: list[str] | None = None,
) -> RunResult:
    """Replay `policy` on master days in [start, end], starting fully in cash."""
    days = master[(master >= start) & (master <= end)]
    if days.empty:
        raise ValueError("no master day in the run window")
    assets = policy.assets
    units = dict.fromkeys(assets, 0)
    last_px: dict[str, float | None] = {a: _latest(prices[a], days[0]) for a in assets}
    pnl = {a: {"price": 0.0, "commission": 0.0, "spread": 0.0, "slippage": 0.0} for a in assets}
    pending: dict[str, Order] = {}
    cash = float(capital)
    rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    prev_nav = cash
    prev_day: pd.Timestamp | None = None
    for i, day in enumerate(days):
        day = pd.Timestamp(day)
        earned = interest(cash, cash_rate, prev_day, day) if prev_day is not None else 0.0
        cash += earned
        price_pnl = 0.0
        for a in assets:
            px = _price_on(prices[a], day)
            if px is None:
                continue
            prev_px = last_px[a]
            if prev_px is not None and units[a]:
                move = units[a] * (px - prev_px)
                pnl[a]["price"] += move
                price_pnl += move
            last_px[a] = px
        day_costs = {"commission": 0.0, "spread": 0.0, "slippage": 0.0}
        fills = [o for a, o in sorted(pending.items()) if _price_on(prices[a], day) is not None]
        for order in sorted(fills, key=lambda o: (o.quantity > 0, o.asset)):  # sells first
            a = order.asset
            px = float(last_px[a])  # type: ignore[arg-type]
            qty = order.quantity
            hs, sl = costs.half_spread[a], costs.slippage[a]
            if qty > 0:
                per_unit = px * (1 + hs + sl)
                qty = min(qty, math.floor(cash / per_unit) if per_unit > 0 else 0)
                while qty > 0 and qty * per_unit + costs.commission(qty * px) > cash:
                    qty -= 1
            del pending[a]
            if qty == 0:
                trades.append(
                    {
                        "date": day,
                        "asset": a,
                        "quantity": 0,
                        "requested": order.quantity,
                        "decided": order.decided,
                        "status": "dropped_no_cash",
                    }
                )
                continue
            value = abs(qty) * px
            fee = costs.commission(value)
            spread, slip = value * hs, value * sl
            cash -= qty * px + fee + spread + slip
            units[a] += qty
            for k, v in (("commission", fee), ("spread", spread), ("slippage", slip)):
                pnl[a][k] += v
                day_costs[k] += v
            trades.append(
                {
                    "date": day,
                    "asset": a,
                    "quantity": qty,
                    "requested": order.quantity,
                    "decided": order.decided,
                    "price": px,
                    "value": value,
                    "commission": fee,
                    "spread": spread,
                    "slippage": slip,
                    "status": "filled",
                }
            )
        if cash < -TOLERANCE:
            raise AccountingError(f"negative cash on {day.date()}")
        holdings = sum(units[a] * float(last_px[a] or 0.0) for a in assets)
        nav = cash + holdings
        expected = prev_nav + price_pnl + earned - sum(day_costs.values())
        if i > 0 and abs(nav - expected) > TOLERANCE * max(1.0, abs(nav)):
            raise AccountingError(f"NAV identity broken on {day.date()}: {nav} vs {expected}")
        rows.append(
            {
                "date": day,
                "nav": nav,
                "cash": cash,
                "holdings": holdings,
                "interest": earned,
                "price_pnl": price_pnl,
                **day_costs,
                "exposure": holdings / nav if nav > 0 else 0.0,
            }
        )
        ctx = Context(
            day=day,
            prev_day=prev_day,
            first=i == 0,
            nav=nav,
            units=dict(units),
            prices={a: float(p) for a, p in last_px.items() if p is not None},
            pending=frozenset(pending),
        )
        for a, weight in sorted(policy.decide(ctx).items()):
            if a in pending:
                continue
            px_now = last_px[a]
            if px_now is None:
                continue
            delta = whole_units(weight, nav, px_now) - units[a]
            if delta != 0:
                pending[a] = Order(a, delta, day)
        prev_nav, prev_day = nav, day
    daily = pd.DataFrame(rows).set_index("date")
    total_asset = sum(
        p["price"] - p["commission"] - p["spread"] - p["slippage"] for p in pnl.values()
    )
    total = float(daily["nav"].iloc[-1]) - capital
    if abs(total - daily["interest"].sum() - total_asset) > TOLERANCE * max(1.0, capital) * 10:
        raise AccountingError("asset P&L contributions do not sum to total P&L minus interest")
    left = [
        {"asset": o.asset, "quantity": o.quantity, "decided": o.decided} for o in pending.values()
    ]
    return RunResult(daily, trades, pnl, list(flags or []), left)
