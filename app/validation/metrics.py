"""Vectorised metrics on return matrices (rows = sessions, columns = series).

Definitions are identical to Phase 8 (`app.backtest.metrics`; tested for equality on the
original sample): A = periods per year, rf = 0, std ddof = 1.
"""

import numpy as np

METRICS = (
    "cumulative_return",
    "cagr",
    "annualized_volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "annualized_mean_return",
)


def matrix_metrics(r: np.ndarray, periods: int) -> dict[str, np.ndarray]:
    """r: (n, k) simple returns. Returns metric -> (k,) array (NaN where undefined)."""
    n = r.shape[0]
    with np.errstate(divide="ignore", invalid="ignore"):
        growth = np.prod(1.0 + r, axis=0)
        cumulative = growth - 1.0
        cagr = (
            np.where(growth > 0, growth ** (periods / n) - 1.0, np.nan)
            if n
            else np.full(r.shape[1], np.nan)
        )
        mean = r.mean(axis=0)
        std = r.std(axis=0, ddof=1) if n >= 2 else np.full(r.shape[1], np.nan)
        vol = std * np.sqrt(periods)
        sharpe = np.where(std > 0, mean / std * np.sqrt(periods), np.nan)
        downside = np.sqrt((np.minimum(r, 0.0) ** 2).mean(axis=0))
        sortino = np.where(downside > 0, mean / downside * np.sqrt(periods), np.nan)
        equity = np.cumprod(1.0 + r, axis=0)
        peak = np.maximum(np.maximum.accumulate(equity, axis=0), 1.0)  # peak includes start = 1
        max_dd = (equity / peak - 1.0).min(axis=0)
    return {
        "cumulative_return": cumulative,
        "cagr": cagr,
        "annualized_volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "annualized_mean_return": mean * periods,
    }


def concentration(r: np.ndarray, top_days: int = 10) -> dict[str, float]:
    """How much a single series depends on a few sessions (descriptive)."""
    finite = r[np.isfinite(r)]
    full = float(np.prod(1 + finite) - 1)
    order = np.argsort(finite)[::-1]
    without_best = finite.copy()
    without_best[order[:top_days]] = 0.0
    log_r = np.log1p(finite)
    k = max(1, int(np.ceil(0.01 * finite.size)))
    total_log = float(log_r.sum())
    return {
        "cumulative_return": full,
        f"cumulative_return_without_best_{top_days}_days": float(np.prod(1 + without_best) - 1),
        "log_return_share_of_top_1pct_days": float(np.sort(log_r)[::-1][:k].sum() / total_log)
        if total_log > 0
        else float("nan"),
    }
