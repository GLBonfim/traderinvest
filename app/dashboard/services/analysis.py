"""Engine outputs (Phases 3-7) and point-in-time views "as of" a selected session.

Every view at session T reads only rows available at the close of T:
- candle geometry/patterns stamped at T (a pattern is stamped at its LAST candle);
- price-action state of T; swings only if `confirmed_at <= T` (confirmation latency: a pivot
  is never shown as known before it was confirmed); events only if `available_at <= T`;
  support/resistance from the state row of T (the engine's `zones` table is "as of the last
  bar" and is therefore NOT used for historical sessions);
- indicator and regime rows of T; strategy signal rows of T.
"""

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.candles.engine import CandleAnalysis, CandlestickEngine
from app.dashboard.services.market import position
from app.indicators.engine import IndicatorAnalysis, IndicatorEngine
from app.price_action.engine import PriceActionAnalysis, PriceActionEngine
from app.regimes.engine import RegimeAnalysis, RegimeEngine
from app.strategies.engine import StrategyEngine, StrategyRun

OHLCV = ["open", "high", "low", "close", "volume"]
REGIME_DIMENSIONS = {
    "trend": ("trend_regime", "trend_changed", "trend_age"),
    "volatility": ("volatility_regime", "volatility_changed", "volatility_age"),
    "momentum": ("momentum_regime", "momentum_changed", "momentum_age"),
    "participation": ("participation_regime", "participation_changed", "participation_age"),
    "composite": ("composite_regime", "composite_changed", "composite_age"),
}
INDICATOR_GROUPS = {
    "Trend": (
        "sma_20",
        "sma_50",
        "sma_200",
        "ema_20",
        "ema_50",
        "ema_200",
        "macd",
        "macd_signal",
        "macd_hist",
    ),
    "Momentum": ("rsi_14", "stoch_k", "stoch_d", "roc_12"),
    "Volatility": (
        "atr_14",
        "realized_vol_20",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "bb_width_pct",
        "bb_percent_b",
    ),
    "Volume": ("relative_volume_20", "obv"),
}
STRATEGIES = (
    "buy_and_hold",
    "sma_trend",
    "sma_crossover",
    "rsi_momentum",
    "price_action_trend",
    "regime_trend",
)


@dataclass(frozen=True)
class EngineBundle:
    candles: CandleAnalysis
    price_action: PriceActionAnalysis
    indicators: IndicatorAnalysis
    regimes: RegimeAnalysis
    strategies: StrategyRun


def compute_engines(bars: pd.DataFrame) -> EngineBundle:
    """Runs the existing engines on raw OHLCV (adj_close is never an engine input)."""
    ohlcv = bars[OHLCV]
    return EngineBundle(
        candles=CandlestickEngine().analyze(ohlcv),
        price_action=PriceActionEngine().analyze(ohlcv),
        indicators=IndicatorEngine().analyze(ohlcv),
        regimes=RegimeEngine().analyze(ohlcv),
        strategies=StrategyEngine().run(ohlcv),
    )


def _row(frame: pd.DataFrame, ts: pd.Timestamp) -> pd.Series:
    return frame.iloc[position(frame.index, ts)]


def _clean(v: Any) -> Any:
    """NaN/NA -> None (undefined stays undefined; never a fabricated number)."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, float | np.floating) and np.isnan(v):
        return None
    if isinstance(v, np.generic):
        return v.item()
    return v


# ── candles ──


def candle_view(b: EngineBundle, ts: pd.Timestamp) -> dict[str, Any]:
    g = _row(b.candles.geometry, ts)
    obs = b.candles.observations
    hits = obs[obs["ts"] == ts]
    keys = (
        "pattern",
        "family",
        "orientation",
        "bars_in_pattern",
        "strength",
        "context_requirements_met",
        "prior_trend",
        "body_ratio",
        "upper_wick_ratio",
        "lower_wick_ratio",
    )
    return {
        "geometry": {
            k: _clean(g[k])
            for k in (
                "body_ratio",
                "upper_wick_ratio",
                "lower_wick_ratio",
                "close_position",
                "direction",
                "gap",
                "relative_volume",
                "prior_trend",
                "trend_score",
                "usable",
            )
        },
        "patterns": [{k: _clean(r[k]) for k in keys} for _, r in hits.iterrows()],
        "engine_version": b.candles.engine_version,
        "config_fingerprint": b.candles.config_fingerprint,
    }


# ── price action ──

PA_STATE_KEYS = (
    "structure",
    "trend",
    "trend_quality",
    "trend_evidence",
    "last_high_label",
    "last_low_label",
    "last_swing_high",
    "last_swing_low",
    "support_low",
    "support_high",
    "support_distance_pct",
    "resistance_low",
    "resistance_high",
    "resistance_distance_pct",
    "in_consolidation",
    "range_start",
    "range_high",
    "range_low",
    "range_width_pct",
    "range_bar_count",
    "window_width_state",
    "candle_range_state",
)
EVENT_TYPES = ("breakout", "breakdown", "retest", "rejection", "sweep")


def swings_known_at(b: EngineBundle, ts: pd.Timestamp) -> pd.DataFrame:
    s = b.price_action.swings
    return s[s["confirmed_at"] <= ts].reset_index(drop=True)


def events_known_at(b: EngineBundle, ts: pd.Timestamp) -> pd.DataFrame:
    e = b.price_action.events
    return e[e["available_at"] <= ts].reset_index(drop=True)


def price_action_view(
    b: EngineBundle, ts: pd.Timestamp, recent_sessions: int = 20
) -> dict[str, Any]:
    st = _row(b.price_action.state, ts)
    swings = swings_known_at(b, ts)
    events = events_known_at(b, ts)
    idx = b.price_action.state.index
    pos = position(idx, ts)
    window_start = idx[max(0, pos - recent_sessions + 1)]
    recent = events[events["available_at"] >= window_start]
    return {
        "state": {k: _clean(st[k]) for k in PA_STATE_KEYS},
        "recent_swings": swings.tail(8)[
            ["kind", "pivot_ts", "price", "label", "confirmed_at"]
        ].reset_index(drop=True),
        "recent_events": recent[
            [
                "event_type",
                "direction",
                "ts",
                "available_at",
                "reference_level",
                "close",
                "confirmation_type",
            ]
        ].reset_index(drop=True),
        "event_counts": {et: int((recent["event_type"] == et).sum()) for et in EVENT_TYPES},
        "events_today": recent[recent["available_at"] == ts]["event_type"].tolist(),
        "recent_sessions": recent_sessions,
        "engine_version": b.price_action.engine_version,
        "config_fingerprint": b.price_action.config_fingerprint,
    }


# ── indicators ──


def indicator_view(b: EngineBundle, ts: pd.Timestamp) -> pd.DataFrame:
    """Value at T per indicator with its warm-up / undefined status (no fill-in)."""
    v = b.indicators.values
    pos = position(v.index, ts)
    cat = b.indicators.catalog.set_index("column")
    rows = []
    for group, cols in INDICATOR_GROUPS.items():
        for c in cols:
            val = _clean(v[c].iloc[pos])
            first = int(cat["first_valid_index"].loc[c])
            status = "ok" if val is not None else ("warm-up" if pos < first else "undefined")
            rows.append(
                {
                    "group": group,
                    "indicator": c,
                    "value": val,
                    "status": status,
                    "definition": cat.loc[c, "definition"],
                    "warm_up_bars": first,
                }
            )
    return pd.DataFrame(rows)


# ── regimes ──


def regime_view(b: EngineBundle, ts: pd.Timestamp) -> dict[str, Any]:
    s = b.regimes.state
    pos = position(s.index, ts)
    row = s.iloc[pos]
    dims = {}
    for name, (label_col, changed_col, age_col) in REGIME_DIMENSIONS.items():
        labels = s[label_col].iloc[: pos + 1].to_numpy()
        current = labels[-1]
        differ = np.flatnonzero(labels != current)
        previous = labels[differ[-1]] if differ.size else None
        dims[name] = {
            "label": _clean(current),
            "age_sessions": _clean(row[age_col]),
            "changed_on_session": bool(row[changed_col]),
            "previous_label": _clean(previous),
        }
    measures = (
        "trend_quality",
        "volatility_measure",
        "volatility_percentile",
        "volatility_reference_count",
        "atr_pct",
        "momentum_rsi",
        "momentum_roc",
        "momentum_macd",
        "relative_volume",
    )
    return {
        "dimensions": dims,
        "measures": {k: _clean(row[k]) for k in measures},
        "engine_version": b.regimes.engine_version,
        "config_fingerprint": b.regimes.config_fingerprint,
    }


# ── strategies ──


def strategy_states(b: EngineBundle, ts: pd.Timestamp) -> pd.DataFrame:
    sig = b.strategies.signals
    out = []
    for sid in STRATEGIES:
        g = sig[(sig["strategy_id"] == sid) & (sig["bar_ts"] <= ts)]
        if g.empty or g["bar_ts"].iloc[-1] != ts:
            raise KeyError(f"no {sid} state for {ts}")
        cur = g.iloc[-1]
        changes = g[g["changed"]]
        last_change = changes.iloc[-1] if len(changes) else None
        before = None
        if last_change is not None:
            prior = g[g["bar_ts"] < last_change["bar_ts"]]
            before = prior["state"].iloc[-1] if len(prior) else None
        out.append(
            {
                "strategy_id": sid,
                "strategy_version": cur["strategy_version"],
                "state": cur["state"],
                "state_in_effect": cur["state_in_effect"],
                "observed_at": cur["observed_at"],
                "effective_at": cur["effective_at"],
                "reason": cur["reason"],
                "last_transition_at": None if last_change is None else last_change["bar_ts"],
                "last_transition": None
                if last_change is None
                else f"{before or 'start'} -> {last_change['state']}",
            }
        )
    return pd.DataFrame(out)


def strategy_input_series(b: EngineBundle, strategy_id: str) -> pd.Series:
    sig = b.strategies.signals
    g = sig[sig["strategy_id"] == strategy_id]
    return pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"]))


def strategy_version(b: EngineBundle, strategy_id: str) -> str:
    sig = b.strategies.signals
    return str(sig.loc[sig["strategy_id"] == strategy_id, "strategy_version"].iloc[0])
