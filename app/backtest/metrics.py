"""Performance metrics. End-of-sample descriptive statistics, never inputs to positions.

r_t = equity_t / equity_{t-1} - 1 (equity before the first session = initial capital), one per
session of the window, flat days included (r = 0). N = number of sessions, A = periods/year.

    cumulative   = equity_N / initial - 1
    CAGR         = (1 + cumulative)^(A / N) - 1                      NaN if N == 0 or equity <= 0
    ann. vol     = std(r, ddof=1) * sqrt(A)                           NaN if N < 2
    Sharpe       = mean(r - rf) / std(r - rf, ddof=1) * sqrt(A)       NaN if N < 2 or std == 0
    Sortino      = mean(r - rf) / sqrt(mean(min(r - rf, 0)^2)) * sqrt(A)
                                                                      NaN if N < 2 or no downside
    drawdown_t   = equity_t / max(initial, equity_0..t) - 1
    max DD       = min(drawdown); max DD duration = longest run of consecutive sessions with
                   drawdown < 0 (sessions spent below a previous peak)
    exposure     = share of sessions with an open position at the close
    turnover     = sum(traded notional) / mean(equity); annualised = turnover * A / N
Trade statistics use completed trades only; NaN when there are none.
"""

import math
from typing import Any

import numpy as np
import pandas as pd

NAN = float("nan")


def returns_from_equity(equity: pd.Series, initial: float) -> pd.Series:
    prev = equity.shift(1)
    if len(equity):
        prev.iloc[0] = initial
    return equity / prev - 1


def drawdown(equity: pd.Series, initial: float) -> pd.DataFrame:
    peak = equity.cummax().clip(lower=initial)
    return pd.DataFrame({"running_peak": peak, "drawdown": equity / peak - 1}, index=equity.index)


def _longest_run(mask: np.ndarray) -> int:
    best = run = 0
    for m in mask:
        run = run + 1 if m else 0
        best = max(best, run)
    return best


def return_metrics(
    r: pd.Series, equity: pd.Series, initial: float, periods: int, rf: float
) -> dict[str, float]:
    n = len(r)
    final = float(equity.iloc[-1]) if n else NAN
    cumulative = final / initial - 1 if n else NAN
    cagr = (final / initial) ** (periods / n) - 1 if n and final > 0 else NAN
    excess = r - rf
    std = float(excess.std(ddof=1)) if n >= 2 else NAN
    vol = float(r.std(ddof=1)) * math.sqrt(periods) if n >= 2 else NAN
    sharpe = float(excess.mean()) / std * math.sqrt(periods) if n >= 2 and std > 0 else NAN
    downside = math.sqrt(float((excess.clip(upper=0) ** 2).mean())) if n >= 2 else NAN
    sortino = (
        float(excess.mean()) / downside * math.sqrt(periods) if n >= 2 and downside > 0 else NAN
    )
    dd = drawdown(equity, initial)["drawdown"] if n else pd.Series(dtype=float)
    return {
        "sessions": float(n),
        "final_equity": final,
        "cumulative_return": cumulative,
        "cagr": cagr,
        "annualized_volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": float(dd.min()) if n else NAN,
        "max_drawdown_duration_sessions": float(_longest_run((dd < 0).to_numpy())) if n else NAN,
    }


def trade_metrics(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {k: NAN for k in ("win_rate", "avg_trade_net_return", "median_trade_net_return",
                                 "best_trade_net_return", "worst_trade_net_return",
                                 "avg_holding_sessions", "top5_winners_share_of_gross_profit")} | {
            "completed_trades": 0.0
        }  # fmt: skip
    r = trades["net_return"]
    winners = trades["net_pnl"][trades["net_pnl"] > 0].sort_values(ascending=False)
    profit = float(winners.sum())
    return {
        "completed_trades": float(len(trades)),
        "win_rate": float((r > 0).mean()),
        "avg_trade_net_return": float(r.mean()),
        "median_trade_net_return": float(r.median()),
        "best_trade_net_return": float(r.max()),
        "worst_trade_net_return": float(r.min()),
        "avg_holding_sessions": float(trades["holding_sessions"].mean()),
        "top5_winners_share_of_gross_profit": float(winners.iloc[:5].sum()) / profit
        if profit > 0
        else NAN,
    }


def activity_metrics(equity: pd.DataFrame, periods: int) -> dict[str, float]:
    n = len(equity)
    if not n:
        return {
            "exposure": NAN,
            "orders": 0.0,
            "turnover": NAN,
            "annualized_turnover": NAN,
            "total_costs": 0.0,
        }
    mean_equity = float(equity["equity"].mean())
    turnover = float(equity["traded_notional"].sum()) / mean_equity if mean_equity > 0 else NAN
    return {
        "exposure": float(equity["position"].mean()),
        "orders": float(equity["executed"].sum()),
        "turnover": turnover,
        "annualized_turnover": turnover * periods / n,
        "total_costs": float(equity["costs"].sum()),
    }


def all_metrics(
    equity: pd.DataFrame,
    gross_equity: pd.Series,
    trades: pd.DataFrame,
    initial: float,
    periods: int,
    rf: float,
) -> dict[str, Any]:
    net_r = returns_from_equity(equity["equity"], initial)
    gross_r = returns_from_equity(gross_equity, initial)
    net = return_metrics(net_r, equity["equity"], initial, periods, rf)
    gross = return_metrics(gross_r, gross_equity, initial, periods, rf)
    return {
        **net,
        "gross_cumulative_return": gross["cumulative_return"],
        "gross_cagr": gross["cagr"],
        "gross_sharpe": gross["sharpe"],
        "cost_drag_cumulative": gross["cumulative_return"] - net["cumulative_return"],
        **trade_metrics(trades),
        **activity_metrics(equity, periods),
    }
