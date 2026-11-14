"""Run metrics, block-bootstrap uncertainty, approval gates and verdict (sections 11 and 12)."""

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.trend.simulator import RunResult
from app.validation.resampling import bootstrap_summary, moving_block_indices

BLOCK_LENGTH = 21
N_RESAMPLES = 2000
SEED = 20261114
EDGE = "EDGE"
NO_EDGE = "NO STATISTICAL EDGE FOUND"
INSUFFICIENT = "INSUFFICIENT EVIDENCE"


def cash_returns(daily: pd.DataFrame, cash_rate: pd.Series) -> np.ndarray:
    """EUR-cash return of each master day after the first (rate of the previous day, ACT/360)."""
    days = pd.DatetimeIndex(daily.index)
    out = np.zeros(len(days) - 1)
    for i in range(1, len(days)):
        known = cash_rate.loc[cash_rate.index <= days[i - 1]]
        out[i - 1] = float(known.iloc[-1]) * (days[i] - days[i - 1]).days / 360.0
    return out


def years_between(start: pd.Timestamp, end: pd.Timestamp) -> float:
    return (end - start).days / 365.25


def sharpe(excess: np.ndarray, sessions_per_year: float) -> float:
    if excess.size < 2:
        return float("nan")
    sd = float(np.std(excess, ddof=1))
    return float(np.mean(excess) / sd * math.sqrt(sessions_per_year)) if sd > 0 else float("nan")


def max_drawdown(nav: np.ndarray) -> tuple[float, int]:
    """Maximum drawdown (positive fraction) and the longest underwater stretch in sessions."""
    peak = np.maximum.accumulate(nav)
    dd = 1.0 - nav / peak
    longest = run = 0
    for under in dd > 0:
        run = run + 1 if under else 0
        longest = max(longest, run)
    return float(dd.max()), int(longest)


def subperiod_returns(returns: np.ndarray, parts: int = 4) -> list[float]:
    return [float(np.prod(1 + chunk) - 1) for chunk in np.array_split(returns, parts)]


def summarise(result: RunResult, cash_rate: pd.Series, capital: float) -> dict[str, Any]:
    daily = result.daily
    nav = daily["nav"].to_numpy(dtype="float64")
    days = pd.DatetimeIndex(daily.index)
    years = years_between(days[0], days[-1])
    returns = nav[1:] / nav[:-1] - 1.0
    cash = cash_returns(daily, cash_rate)
    excess = returns - cash
    spy = (len(days) - 1) / years if years > 0 else float("nan")
    total = nav[-1] / capital - 1.0
    cash_total = float(np.prod(1 + cash) - 1.0)
    dd, dd_len = max_drawdown(nav)
    monthly = (1 + pd.Series(returns, index=days[1:])).groupby(days[1:].to_period("M")).prod() - 1
    traded = sum(t.get("value", 0.0) for t in result.trades if t["status"] == "filled")
    avg_nav = float(nav.mean())
    trading_cost = float(daily[["commission", "spread", "slippage"]].to_numpy().sum())
    net_by_asset = {
        a: p["price"] - p["commission"] - p["spread"] - p["slippage"]
        for a, p in result.asset_pnl.items()
    }
    return {
        "start": days[0].date().isoformat(),
        "end": days[-1].date().isoformat(),
        "sessions": len(days),
        "years": years,
        "sessions_per_year": spy,
        "capital": capital,
        "final_nav": float(nav[-1]),
        "total_return": float(total),
        "cagr": float((1 + total) ** (1 / years) - 1) if years > 0 else float("nan"),
        "cash_total_return": cash_total,
        "cash_cagr": float((1 + cash_total) ** (1 / years) - 1) if years > 0 else float("nan"),
        "volatility": float(np.std(returns, ddof=1) * math.sqrt(spy)) if returns.size > 1 else 0.0,
        "sharpe": sharpe(excess, spy),
        "max_drawdown": dd,
        "max_drawdown_sessions": dd_len,
        "worst_month": float(monthly.min()) if len(monthly) else 0.0,
        "trades": sum(1 for t in result.trades if t["status"] == "filled"),
        "dropped_orders": sum(1 for t in result.trades if t["status"] != "filled"),
        "turnover_per_year": traded / avg_nav / years if years > 0 else float("nan"),
        "average_exposure": float(daily["exposure"].mean()),
        "costs": {k: float(daily[k].sum()) for k in ("commission", "spread", "slippage")},
        "interest": float(daily["interest"].sum()),
        "annual_trading_cost_pct": 100 * trading_cost / avg_nav / years if years > 0 else 0.0,
        "pnl_by_asset": net_by_asset,
        "subperiod_returns": subperiod_returns(returns),
        "flags": list(result.flags),
        "_excess": excess,
        "_returns": returns,
    }


def sharpe_samples(excess: np.ndarray, spy: float, idx: np.ndarray) -> np.ndarray:
    resampled = excess[idx]
    mean = resampled.mean(axis=1)
    sd = resampled.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(sd > 0, mean / sd * math.sqrt(spy), np.nan)
    return np.asarray(out, dtype="float64")


def sharpe_uncertainty(
    excess: np.ndarray, spy: float, benchmark_excess: np.ndarray | None = None
) -> dict[str, Any]:
    """One-sided 95 % lower bound (5th percentile of finite samples), two-sided 95 % interval,
    and the paired interval of the Sharpe difference against a benchmark when given."""
    idx = moving_block_indices(excess.size, BLOCK_LENGTH, N_RESAMPLES, SEED)
    samples = sharpe_samples(excess, spy, idx)
    finite = samples[np.isfinite(samples)]
    out: dict[str, Any] = {
        "lower_one_sided_95": float(np.percentile(finite, 5)) if finite.size else float("nan"),
        "two_sided_95": bootstrap_summary(samples, 0.95).__dict__,
    }
    if benchmark_excess is not None:
        diff = samples - sharpe_samples(benchmark_excess, spy, idx)
        out["difference_vs_benchmark_95"] = bootstrap_summary(diff, 0.95).__dict__
    return out


@dataclass(frozen=True)
class Gate:
    number: int
    name: str
    passed: bool
    detail: str


def gates(
    strategy: dict[str, Any],
    strategy_double: dict[str, Any],
    equal_weight: dict[str, Any],
) -> list[Gate]:
    """The six all-must-pass gates of section 12 (realistic costs, reference capital)."""
    positive = sum(v for v in strategy["pnl_by_asset"].values() if v > 0)
    top = max(strategy["pnl_by_asset"].values(), default=0.0)
    share = top / positive if positive > 0 else float("inf")
    non_negative = sum(1 for r in strategy["subperiod_returns"] if r >= 0)
    return [
        Gate(
            1,
            "CAGR above EUR cash",
            strategy["cagr"] > strategy["cash_cagr"],
            f"{strategy['cagr']:.4%} vs {strategy['cash_cagr']:.4%}",
        ),
        Gate(
            2,
            "Sharpe >= 0.40 and above equal weight",
            strategy["sharpe"] >= 0.40 and strategy["sharpe"] > equal_weight["sharpe"],
            f"{strategy['sharpe']:.3f} vs equal weight {equal_weight['sharpe']:.3f}",
        ),
        Gate(
            3,
            "Max drawdown <= 20 % and below equal weight",
            strategy["max_drawdown"] <= 0.20
            and strategy["max_drawdown"] < equal_weight["max_drawdown"],
            f"{strategy['max_drawdown']:.2%} vs equal weight {equal_weight['max_drawdown']:.2%}",
        ),
        Gate(
            4,
            "Positive with double costs",
            strategy_double["total_return"] > 0,
            f"{strategy_double['total_return']:.4%}",
        ),
        Gate(
            5, "At least 3 of 4 sub-periods non-negative", non_negative >= 3, f"{non_negative} of 4"
        ),
        Gate(
            6,
            "No asset above 40 % of positive P&L",
            positive > 0 and share <= 0.40,
            f"largest share {share:.1%}" if positive > 0 else "no positive asset P&L",
        ),
    ]


def verdict(gate_list: list[Gate], lower_bound: float, evaluation_failed: bool = False) -> str:
    if evaluation_failed:
        return INSUFFICIENT
    if not all(g.passed for g in gate_list):
        return NO_EDGE
    return EDGE if np.isfinite(lower_bound) and lower_bound > 0 else INSUFFICIENT
