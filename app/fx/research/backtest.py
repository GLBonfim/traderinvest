"""Bar backtester with bid/ask fills, for one pair and one signal stream.

Conventions (conservative on purpose):
- a signal at bar t's close is executed at bar t+1's OPEN on the real side of the book (buy at
  the ask, sell at the bid); no entry across a data gap (weekend, outage);
- stop = entry -/+ stop_dist, target = entry +/- target_dist; exits are checked on the side
  that closes the position (a long sells at the bid, a short buys at the ask);
- a bar opening beyond the stop fills at that open (gap); if stop and target both lie inside
  one bar the STOP is assumed first;
- time stop after `max_hold` bars and forced exit before a weekend/data gap, at the close;
- one position per pair, no pyramiding, no re-entry decision before the exit is known.

Results are in R (multiples of the initial risk = stop distance). Costs in R:
- spread: already inside the bid/ask fills;
- commission per side = max(min per order, bps of notional) — IBKR-like defaults, to confirm
  against the owner's commission schedule;
- financing per 17:00 New York rollover crossed, in bps of notional (conservative, carry
  ignored).
The min-commission share depends on the position size, from `account_equity` x `risk_pct`;
cross rates for that conversion are approximate constants (documented, minor effect).
"""

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from app.fx.models import pair

# approximate conversions for the per-order commission minimum (USD -> quote currency) and
# for the position size (EUR account -> quote currency)
USD_TO = {"USD": 1.0, "JPY": 150.0, "CHF": 0.85, "CAD": 1.37, "GBP": 0.77}
EUR_TO = {"USD": 1.10, "JPY": 165.0, "CHF": 0.94, "CAD": 1.50, "GBP": 0.85}


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 0.20  # per side, of notional
    commission_min_usd: float = 2.0  # per order
    financing_bps_per_night: float = 0.5
    extra_slippage_pips: float = 0.0  # sensitivity only: bid/ask fills already pay the spread
    account_equity: float = 10_000.0  # EUR
    risk_pct: float = 0.5


DEFAULT_COSTS = CostModel()

TRADE_COLUMNS = [
    "pair",
    "dir",
    "signal_ts",
    "entry_ts",
    "exit_ts",
    "entry",
    "exit",
    "stop_dist",
    "target_dist",
    "reason",
    "bars",
    "nights",
    "r_gross",
    "r_cost",
    "r_net",
]


def _trading_day_ordinal(index: pd.DatetimeIndex) -> np.ndarray:
    ny = index.tz_convert("America/New_York") + pd.Timedelta(hours=7)
    return np.asarray(ny.tz_localize(None).to_numpy().astype("datetime64[D]").astype(np.int64))


def _ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Epoch nanoseconds whatever the index resolution (pandas 3 may use microseconds)."""
    utc = index.tz_convert("UTC").tz_localize(None)
    return np.asarray(utc.to_numpy().astype("datetime64[ns]").astype(np.int64))


def run_backtest(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    symbol: str,
    allowed: pd.Series | None = None,
    costs: CostModel = DEFAULT_COSTS,
    gap: timedelta = timedelta(hours=2),
) -> pd.DataFrame:
    """Trades of one pair. `df` has bid_/ask_ OHLC columns indexed by bar open (UTC);
    `signals` has dir, stop_dist, target_dist, max_hold aligned on the same index;
    `allowed` (bool) masks where entries may be decided."""
    p = pair(symbol)
    idx = pd.DatetimeIndex(df.index)
    n = len(idx)
    bo, bh, bl, bc = (df[f"bid_{k}"].to_numpy(float) for k in ("open", "high", "low", "close"))
    ao, ah, al, ac = (df[f"ask_{k}"].to_numpy(float) for k in ("open", "high", "low", "close"))
    sd = signals["dir"].to_numpy(int)
    ss = signals["stop_dist"].to_numpy(float)
    st = signals["target_dist"].to_numpy(float)
    sh = signals["max_hold"].to_numpy(int)
    ok = np.ones(n, bool) if allowed is None else allowed.reindex(idx).fillna(False).to_numpy(bool)
    ts = _ns(idx)
    gap_ns = int(gap.total_seconds() * 1e9)
    next_gap = np.ones(n, bool)
    next_gap[:-1] = (ts[1:] - ts[:-1]) > gap_ns
    tday = _trading_day_ordinal(idx)
    slip = costs.extra_slippage_pips * p.pip_size
    risk_q = costs.account_equity * costs.risk_pct / 100 * EUR_TO.get(p.quote, 1.0)
    min_comm_q = costs.commission_min_usd * USD_TO.get(p.quote, 1.0)

    rows: list[tuple[object, ...]] = []
    pos: tuple[int, int, float, float, float, int, int] | None = None
    for j in range(n):
        if pos is not None:
            d, e_bar, entry, stop, target, deadline, sig_bar = pos
            if d > 0:
                op, hi, lo, cl = bo[j], bh[j], bl[j], bc[j]
            else:
                op, hi, lo, cl = ao[j], ah[j], al[j], ac[j]
            px, why = np.nan, ""
            if d > 0:
                if j > e_bar and op <= stop:
                    px, why = op, "stop_gap"
                elif lo <= stop:
                    px, why = stop, "stop"
                elif j > e_bar and op >= target:
                    px, why = op, "target_gap"
                elif hi >= target:
                    px, why = target, "target"
            else:
                if j > e_bar and op >= stop:
                    px, why = op, "stop_gap"
                elif hi >= stop:
                    px, why = stop, "stop"
                elif j > e_bar and op <= target:
                    px, why = op, "target_gap"
                elif lo <= target:
                    px, why = target, "target"
            if not why and (j >= deadline or next_gap[j]):
                px, why = cl, ("time" if j >= deadline else "weekend")
            if why:
                px = px - d * slip
                sdist = ss[sig_bar]
                r_gross = d * (px - entry) / sdist
                price = entry
                units = risk_q / sdist
                comm_q = 2 * max(min_comm_q, costs.commission_bps * 1e-4 * units * price)
                nights = int(tday[j] - tday[e_bar])
                fin_q = nights * costs.financing_bps_per_night * 1e-4 * units * price
                r_cost = (comm_q + fin_q) / risk_q
                rows.append(
                    (
                        symbol,
                        d,
                        idx[sig_bar],
                        idx[e_bar],
                        idx[j],
                        entry,
                        px,
                        sdist,
                        st[sig_bar],
                        why,
                        j - e_bar + 1,
                        nights,
                        r_gross,
                        r_cost,
                        r_gross - r_cost,
                    )
                )
                pos = None
        if pos is None and j < n - 1 and sd[j] != 0 and ok[j] and not next_gap[j]:
            if not (np.isfinite(ss[j]) and ss[j] > 0 and np.isfinite(st[j]) and st[j] > 0):
                continue
            d = int(sd[j])
            e = j + 1
            entry = (ao[e] if d > 0 else bo[e]) + d * slip
            stop = entry - d * ss[j]
            target = entry + d * st[j]
            pos = (d, e, entry, stop, target, e + max(1, int(sh[j])) - 1, j)
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def summarize(trades: pd.DataFrame, days: float | None = None) -> dict[str, float]:
    """Trade statistics in R (net of costs)."""
    r = trades["r_net"].to_numpy(float) if len(trades) else np.array([])
    n = len(r)
    if n == 0:
        return {
            "trades": 0,
            "mean_r": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_r": 0.0,
            "max_dd_r": 0.0,
            "t_stat": 0.0,
            "mean_cost_r": 0.0,
        }
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    equity = np.concatenate([[0.0], np.cumsum(r)])  # the curve starts at 0 R (no trade yet)
    dd = float(np.max(np.maximum.accumulate(equity) - equity))
    sd = float(np.std(r, ddof=1)) if n > 1 else 0.0
    out = {
        "trades": float(n),
        "mean_r": float(r.mean()),
        "median_r": float(np.median(r)),
        "win_rate": float((r > 0).mean()),
        "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
        "total_r": float(r.sum()),
        "max_dd_r": dd,
        "t_stat": float(r.mean() / sd * np.sqrt(n)) if sd > 0 else 0.0,
        "mean_cost_r": float(trades["r_cost"].mean()),
        "mean_bars": float(trades["bars"].mean()),
    }
    if days:
        out["trades_per_day"] = n / days
    return out
