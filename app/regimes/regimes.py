"""Pure, causal regime classification functions. Row t only ever uses rows <= t.

Regime labels describe observed market conditions; they are not trading signals or predictions.
"""

import numpy as np
import pandas as pd

from app.regimes.config import RegimeConfig

INSUFFICIENT = "insufficient_data"
ROUND = 10


def causal_percentile_rank(x: pd.Series, lookback: int, min_observations: int) -> pd.DataFrame:
    """Mid-rank percentile of x[t] within the PREVIOUS `lookback` values (t itself excluded).

        reference(t) = valid x[t-lookback .. t-1]
        pct(t) = (#{r < x[t]} + 0.5 * #{r == x[t]}) / #reference      (no interpolation)

    NaN when x[t] is NaN or the reference has fewer than `min_observations` valid values.
    Ties count half, so a constant series has percentile 0.5.
    """
    values = x.to_numpy(dtype=np.float64)
    pct = np.full(values.shape, np.nan)
    count = np.zeros(values.shape, dtype=np.int64)
    for t in range(len(values)):
        ref = values[max(0, t - lookback) : t]
        ref = ref[~np.isnan(ref)]
        count[t] = ref.size
        if np.isnan(values[t]) or ref.size < min_observations:
            continue
        below = np.count_nonzero(ref < values[t])
        equal = np.count_nonzero(ref == values[t])
        pct[t] = round((below + 0.5 * equal) / ref.size, ROUND)
    return pd.DataFrame({"percentile": pct, "reference_count": count}, index=x.index)


def volatility_regime(percentile: pd.Series, cfg: RegimeConfig) -> pd.Series:
    p = percentile
    return pd.Series(
        np.select(
            [
                p.isna(),
                p >= cfg.volatility_extreme_from,
                p >= cfg.volatility_high_from,
                p < cfg.volatility_low_below,
            ],
            [INSUFFICIENT, "extreme", "high", "low"],
            "normal",
        ),
        index=p.index,
    )


def momentum_regime(
    rsi: pd.Series, roc: pd.Series, macd: pd.Series, cfg: RegimeConfig
) -> pd.Series:
    """positive: RSI > mid AND ROC > 0 AND MACD > 0 (all three agree); negative: mirror.
    extreme_positive: positive AND RSI >= extreme_high; extreme_negative: negative AND
    RSI <= extreme_low. neutral: any disagreement or a component exactly at its midpoint."""
    missing = rsi.isna() | roc.isna() | macd.isna()
    positive = (rsi > cfg.momentum_rsi_mid) & (roc > 0) & (macd > 0)
    negative = (rsi < cfg.momentum_rsi_mid) & (roc < 0) & (macd < 0)
    return pd.Series(
        np.select(
            [
                missing,
                positive & (rsi >= cfg.momentum_rsi_extreme_high),
                negative & (rsi <= cfg.momentum_rsi_extreme_low),
                positive,
                negative,
            ],
            [INSUFFICIENT, "extreme_positive", "extreme_negative", "positive", "negative"],
            "neutral",
        ),
        index=rsi.index,
    )


def participation_state(relative_volume: pd.Series, cfg: RegimeConfig) -> pd.Series:
    """Instrument-level volume participation from relative volume (NOT market breadth)."""
    rv = relative_volume
    return pd.Series(
        np.select(
            [rv.isna(), rv >= cfg.participation_high_from, rv < cfg.participation_low_below],
            [INSUFFICIENT, "high", "low"],
            "normal",
        ),
        index=rv.index,
    )


# Composite = f(trend, volatility). Momentum and participation stay separate dimensions.
COMPOSITE_BY_TREND = {"uptrend": "trending_up", "downtrend": "trending_down", "range": "ranging"}
COMPOSITE_TRANSITION_BY_VOL = {
    "low": "low_volatility_transition",
    "normal": "transition",
    "high": "high_volatility_transition",
    "extreme": "high_volatility_transition",
}


def composite_regime(trend: pd.Series, volatility: pd.Series) -> pd.Series:
    out = []
    for tr, vol in zip(trend, volatility, strict=True):
        if tr == INSUFFICIENT or vol == INSUFFICIENT:
            out.append(INSUFFICIENT)
        elif tr in COMPOSITE_BY_TREND:
            out.append(COMPOSITE_BY_TREND[tr])
        elif tr == "transition":
            out.append(COMPOSITE_TRANSITION_BY_VOL[vol])
        else:
            raise ValueError(f"unknown trend state {tr!r}")
    return pd.Series(out, index=trend.index, dtype=object)


def transitions(state: pd.Series) -> pd.DataFrame:
    """changed(t) = state(t) != state(t-1) (False on the first bar);
    age(t) = consecutive bars, including t, with the same state. Both causal."""
    changed = state.ne(state.shift(1))
    changed.iloc[:1] = False
    run_id = state.ne(state.shift(1)).cumsum()
    age = state.groupby(run_id).cumcount() + 1
    return pd.DataFrame(
        {"changed": changed.astype(bool), "age": age.astype("int64")}, index=state.index
    )
