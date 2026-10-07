"""Event-driven single-instrument simulation (LONG = fully invested, FLAT = all cash).

For each session t of the window, in order:
  1. if the target decided at the close of t-1 differs from the current position, execute ONE
     order at the open of t (validated: after the decision's effective_at, at the open price);
  2. mark the portfolio to market at the close of t.
Nothing at t reads data from t+1 or later.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.execution import ExecutionModel, FillQuote, buy_notional, order_costs
from app.backtest.models import Costs, Fill, OpenPosition, Trade
from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG

EPS_SHARES = 1e-12


def target_positions(states: Sequence[str]) -> np.ndarray:
    """Target position decided at each bar: LONG -> 1, FLAT -> 0, INSUFFICIENT_DATA -> keep the
    previous target (0 before the first valid state). INSUFFICIENT_DATA never opens or closes."""
    out = np.zeros(len(states))
    current = 0.0
    for i, s in enumerate(states):
        if s == LONG:
            current = 1.0
        elif s == FLAT:
            current = 0.0
        elif s != INSUFFICIENT_DATA:
            raise ValueError(f"unknown state {s!r}")
        out[i] = current
    return out


@dataclass
class Simulation:
    equity: pd.DataFrame
    fills: list[Fill]
    trades: list[Trade]
    open_position: OpenPosition | None
    pending: dict[str, Any] | None


def simulate(
    *,
    strategy_id: str,
    index: pd.DatetimeIndex,
    opens: np.ndarray,
    closes: np.ndarray,
    states: Sequence[str],
    observed_at: Sequence[pd.Timestamp],
    effective_at: Sequence[pd.Timestamp],
    window_start: int,
    cfg: BacktestConfig,
    model: ExecutionModel,
) -> Simulation:
    n = len(index)
    targets = target_positions(states)
    cash, shares = float(cfg.initial_capital), 0.0
    fills: list[Fill] = []
    trades: list[Trade] = []
    open_entry: Fill | None = None
    rows: list[dict[str, Any]] = []

    for t in range(window_start, n):
        cost_today, traded_today, executed = 0.0, 0.0, False
        if t >= 1:
            d = t - 1  # decision bar
            want_long = targets[d] == 1.0
            if want_long != (shares > EPS_SHARES):
                quote = _validate_quote(model.quote(d, opens), d, t, index, opens, effective_at)
                prev = _previous_valid_state(states, d)
                reason = f"{prev}->{states[d]} observed {pd.Timestamp(observed_at[d]).isoformat()}"
                if want_long:
                    notional = buy_notional(cash, cfg)
                    costs = order_costs(notional, cfg)
                    shares = notional / quote.price
                    cash -= notional + costs.total
                    fill = Fill(t, index[t], "buy", quote.price, shares, notional, costs,
                                pd.Timestamp(observed_at[d]), reason)  # fmt: skip
                    open_entry = fill
                else:
                    notional = shares * quote.price
                    costs = order_costs(notional, cfg)
                    cash += notional - costs.total
                    fill = Fill(t, index[t], "sell", quote.price, shares, notional, costs,
                                pd.Timestamp(observed_at[d]), reason)  # fmt: skip
                    if open_entry is None:
                        raise RuntimeError("sell without an open position")
                    trades.append(Trade(len(trades), strategy_id, open_entry, fill))
                    open_entry, shares = None, 0.0
                fills.append(fill)
                cost_today, traded_today, executed = costs.total, notional, True

        equity = cash + shares * closes[t]
        rows.append(
            {
                "ts": index[t],
                "state": states[t],
                "state_in_effect": _state_in_effect(states, t),
                "position": 1.0 if shares > EPS_SHARES else 0.0,
                "shares": shares,
                "open": opens[t],
                "close": closes[t],
                "executed": executed,
                "traded_notional": traded_today,
                "costs": cost_today,
                "cash": cash,
                "equity": equity,
            }
        )

    equity_df = pd.DataFrame(rows).set_index("ts") if rows else _empty_equity()
    open_position = (
        OpenPosition(
            strategy_id, open_entry, pd.Timestamp(observed_at[n - 1]), float(closes[n - 1])
        )
        if open_entry is not None and n
        else None
    )
    pending = None
    if n and (targets[n - 1] == 1.0) != (shares > EPS_SHARES):
        pending = {
            "state": states[n - 1],
            "observed_at": pd.Timestamp(observed_at[n - 1]),
            "effective_at": pd.Timestamp(effective_at[n - 1]),
            "status": "not_executed_no_next_session_in_data",
        }
    return Simulation(equity_df, fills, trades, open_position, pending)


def _validate_quote(
    quote: FillQuote | None,
    decision: int,
    t: int,
    index: pd.DatetimeIndex,
    opens: np.ndarray,
    effective_at: Sequence[pd.Timestamp],
) -> FillQuote:
    """Rejects any execution that is not at the open of the session the decision applies to."""
    if quote is None:
        raise ValueError("execution model returned no fill for an executable decision")
    if quote.bar_index <= decision:
        raise ValueError("execution at or before the decision bar (look-ahead)")
    if quote.bar_index != t:
        raise ValueError("execution must happen at the first session after the decision")
    if index[quote.bar_index] < pd.Timestamp(effective_at[decision]):
        raise ValueError("execution before the decision's effective_at")
    if index[quote.bar_index] != pd.Timestamp(effective_at[decision]):
        raise ValueError("next bar is not the next exchange session (missing session in data)")
    if quote.price != opens[quote.bar_index]:
        raise ValueError("execution price must be the session open")
    return quote


def _previous_valid_state(states: Sequence[str], d: int) -> str:
    for i in range(d - 1, -1, -1):
        if states[i] in (LONG, FLAT):
            return states[i]
    return "NONE"


def _state_in_effect(states: Sequence[str], t: int) -> str:
    return states[t - 1] if t >= 1 else INSUFFICIENT_DATA


def _empty_equity() -> pd.DataFrame:
    cols = ["state", "state_in_effect", "position", "shares", "open", "close", "executed",
            "traded_notional", "costs", "cash", "equity"]  # fmt: skip
    return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="ts"))


def costs_frame(fills: Sequence[Fill]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts": f.ts,
                "side": f.side,
                "price": f.price,
                "shares": f.shares,
                "notional": f.notional,
                "commission": f.costs.commission,
                "spread_cost": f.costs.spread,
                "slippage_cost": f.costs.slippage,
                "total_cost": f.costs.total,
                "signal_observed_at": f.signal_observed_at,
                "reason": f.reason,
            }
            for f in fills
        ],
        columns=[
            "ts",
            "side",
            "price",
            "shares",
            "notional",
            "commission",
            "spread_cost",
            "slippage_cost",
            "total_cost",
            "signal_observed_at",
            "reason",
        ],
    )


__all__ = ["Costs", "Simulation", "costs_frame", "simulate", "target_positions"]
