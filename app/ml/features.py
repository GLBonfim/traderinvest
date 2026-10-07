"""Point-in-time ML feature matrix built from the existing engines (no formula re-implemented).

Every feature of the row for bar T is known at the CLOSE of T (`observed_at`):
- candle geometry/patterns of bars <= T (a pattern is stamped at its LAST candle);
- Price Action state after bar T (pivots only from `confirmed_at`), events with
  `available_at == T`; the Price Action OUTCOMES table is NEVER used (known only later);
- indicator values at T (trailing windows);
- regime labels at T.
ML-specific representations (documented, the source engines are unchanged): price-level
series (SMA/EMA, MACD, OBV) are made scale-free (distances, /close, normalised flows) because
SPY's price level changes ~18x over the sample.

Undefined values are never replaced here: REQUIRED features must be finite for a row to be
`valid`; OPTIONAL features may be NaN (handled later by a TRAIN-fitted preprocessor with
explicit missing indicators).
"""

import hashlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.backtest.engine import session_times
from app.candles.engine import CandlestickEngine
from app.candles.patterns import REGISTRY
from app.data.calendar import TradingCalendar
from app.indicators.engine import IndicatorEngine
from app.ml.config import FEATURE_VERSION
from app.price_action.engine import PriceActionEngine
from app.regimes.engine import RegimeEngine

VALID = "valid"
INSUFFICIENT = "insufficient_features"

STRUCTURES = ("uptrend", "downtrend", "range", "transition")
VOL_REGIMES = ("low", "normal", "high", "extreme")
MOM_REGIMES = ("extreme_negative", "negative", "neutral", "positive", "extreme_positive")
PART_REGIMES = ("low", "normal", "high", "insufficient_data")
COMPOSITES = (
    "trending_up",
    "trending_down",
    "ranging",
    "transition",
    "high_volatility_transition",
    "low_volatility_transition",
)
EVENTS = (
    ("breakout", "up"),
    ("breakdown", "down"),
    ("retest", "up"),
    ("retest", "down"),
    ("rejection", "up"),
    ("rejection", "down"),
    ("sweep", "up"),
    ("sweep", "down"),
)


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str  # candle | pattern | indicator | price_action | regime
    kind: str  # numeric | binary
    optional: bool  # may legitimately be undefined (NaN) on a valid row
    definition: str


@dataclass(frozen=True)
class Components:
    """Raw outputs of the existing engines, all computed on bars <= the last bar."""

    geometry: pd.DataFrame
    patterns: pd.DataFrame
    pa_state: pd.DataFrame
    pa_events: pd.DataFrame
    indicators: pd.DataFrame
    regimes: pd.DataFrame


@dataclass(frozen=True)
class FeatureDataset:
    """Authoritative typed feature matrix. `frame` columns = observed_at, availability,
    then exactly the features in `specs` (same order). Index = bar_ts (session open, UTC)."""

    instrument_id: int | None
    timeframe: str
    feature_version: str
    feature_fingerprint: str
    specs: tuple[FeatureSpec, ...]
    frame: pd.DataFrame

    @property
    def feature_names(self) -> list[str]:
        return [s.name for s in self.specs]

    @property
    def X(self) -> pd.DataFrame:  # noqa: N802
        return self.frame[self.feature_names]


def compute_components(bars: pd.DataFrame) -> Components:
    """Runs the existing engines on raw OHLCV only (adj_close is never a feature input)."""
    ohlcv = bars[["open", "high", "low", "close", "volume"]]
    candles = CandlestickEngine().analyze(ohlcv)
    pa = PriceActionEngine().analyze(ohlcv.assign(volume=ohlcv["volume"].fillna(0.0)))
    return Components(
        geometry=candles.geometry,
        patterns=candles.observations,
        pa_state=pa.state,
        pa_events=pa.events,
        indicators=IndicatorEngine().analyze(ohlcv).values,
        regimes=RegimeEngine().analyze(ohlcv).state,
    )


def _onehot(labels: pd.Series, values: tuple[str, ...], prefix: str) -> dict[str, pd.Series]:
    return {f"{prefix}_{v}": (labels == v).astype("float64") for v in values}


def assemble(
    comp: Components,
    close: pd.Series,
    *,
    instrument_id: int | None = None,
    timeframe: str = "1d",
    calendar: TradingCalendar | None = None,
) -> FeatureDataset:
    """Row-wise assembly: every column at T is read from component rows at T only."""
    idx = close.index
    g, ind, pa, rg = comp.geometry, comp.indicators, comp.pa_state, comp.regimes
    cols: dict[str, pd.Series] = {}
    specs: list[FeatureSpec] = []

    def add(
        name: str, s: pd.Series, group: str, kind: str, optional: bool, definition: str
    ) -> None:
        cols[name] = s.reindex(idx).astype("float64")
        specs.append(FeatureSpec(name, group, kind, optional, definition))

    # candle geometry
    add("body_ratio", g["body_ratio"], "candle", "numeric", True, "|C-O|/(H-L); NaN on zero range")
    add("upper_wick_ratio", g["upper_wick_ratio"], "candle", "numeric", True, "(H-max(O,C))/(H-L)")
    add("lower_wick_ratio", g["lower_wick_ratio"], "candle", "numeric", True, "(min(O,C)-L)/(H-L)")
    add("close_position", g["close_position"], "candle", "numeric", True, "(C-L)/(H-L)")
    add("gap", g["gap"], "candle", "numeric", False, "O/C[t-1]-1")
    add("range_pct", g["range"] / close, "candle", "numeric", False, "(H-L)/C")
    add("body_pct", g["body"] / close, "candle", "numeric", False, "(C-O)/C")

    # candle patterns (binary, stamped at the pattern's last candle)
    for spec, _ in REGISTRY:
        hit = comp.patterns.loc[comp.patterns["pattern"] == spec.name, "ts"]
        add(
            f"pattern_{spec.name}",
            pd.Series(idx.isin(pd.DatetimeIndex(hit)), index=idx),
            "pattern",
            "binary",
            False,
            f"1 if {spec.name} observed on this bar",
        )

    # indicators (scale-free representations)
    for n in (20, 50, 200):
        add(
            f"dist_sma_{n}",
            close / ind[f"sma_{n}"] - 1,
            "indicator",
            "numeric",
            False,
            f"C/SMA{n}-1",
        )
        add(
            f"dist_ema_{n}",
            close / ind[f"ema_{n}"] - 1,
            "indicator",
            "numeric",
            False,
            f"C/EMA{n}-1",
        )
    for c in ("macd", "macd_signal", "macd_hist"):
        add(f"{c}_pct", ind[c] / close, "indicator", "numeric", False, f"{c}/C")
    add("rsi_14", ind["rsi_14"], "indicator", "numeric", True, "Wilder RSI14 (NaN if flat)")
    add("stoch_k", ind["stoch_k"], "indicator", "numeric", True, "SMA3 of raw %K14")
    add("stoch_d", ind["stoch_d"], "indicator", "numeric", True, "SMA3 of %K")
    add("roc_12", ind["roc_12"], "indicator", "numeric", False, "100(C/C[t-12]-1)")
    add("atr_pct", ind["atr_14"] / close, "indicator", "numeric", False, "ATR14/C")
    add("bb_width_pct", ind["bb_width_pct"], "indicator", "numeric", False, "(upper-lower)/middle")
    add("bb_percent_b", ind["bb_percent_b"], "indicator", "numeric", True, "%B (NaN if zero width)")
    add(
        "realized_vol_20",
        ind["realized_vol_20"],
        "indicator",
        "numeric",
        False,
        "annualised 20d vol",
    )
    add(
        "relative_volume_20",
        ind["relative_volume_20"],
        "indicator",
        "numeric",
        True,
        "V/mean(V prev 20)",
    )
    obv = ind["obv"]
    vol_sum = comp.geometry["volume"].rolling(20, min_periods=20).sum()
    add(
        "obv_flow_20",
        (obv - obv.shift(20)) / vol_sum.where(vol_sum > 0),
        "indicator",
        "numeric",
        True,
        "(OBV-OBV[t-20])/sum(V last 20)",
    )

    # price action
    for k, v in _onehot(pa["structure"], STRUCTURES, "structure").items():
        add(k, v, "price_action", "binary", False, "structure one-hot (confirmed pivots)")
    add("trend_quality", pa["trend_quality"], "price_action", "numeric", True, "label consistency")
    add(
        "support_distance_pct",
        pa["support_distance_pct"],
        "price_action",
        "numeric",
        True,
        "C/zone_high-1",
    )
    add(
        "resistance_distance_pct",
        pa["resistance_distance_pct"],
        "price_action",
        "numeric",
        True,
        "zone_low/C-1",
    )
    add(
        "support_width_pct",
        (pa["support_high"] - pa["support_low"]) / close,
        "price_action",
        "numeric",
        True,
        "nearest support zone width / C",
    )
    add(
        "in_consolidation",
        pa["in_consolidation"].astype(float),
        "price_action",
        "binary",
        False,
        "20-bar window width <= 5%",
    )
    add(
        "pa_window_width_pct",
        pa["window_width_pct"],
        "price_action",
        "numeric",
        False,
        "20-bar width",
    )
    add(
        "candle_range_ratio",
        pa["candle_range_ratio"],
        "price_action",
        "numeric",
        False,
        "R/mean(R prev 20)",
    )
    add(
        "window_width_ratio",
        pa["window_width_ratio"],
        "price_action",
        "numeric",
        False,
        "width/width[t-20]",
    )
    ev = comp.pa_events
    for etype, direction in EVENTS:
        hit = ev.loc[(ev["event_type"] == etype) & (ev["direction"] == direction), "available_at"]
        add(
            f"event_{etype}_{direction}",
            pd.Series(idx.isin(pd.DatetimeIndex(hit)), index=idx),
            "price_action",
            "binary",
            False,
            f"any {etype} {direction} available at T",
        )

    # regimes
    add(
        "volatility_percentile",
        rg["volatility_percentile"],
        "regime",
        "numeric",
        False,
        "causal pct",
    )
    for k, v in _onehot(rg["volatility_regime"], VOL_REGIMES, "vol").items():
        add(k, v, "regime", "binary", False, "volatility regime one-hot")
    for k, v in _onehot(rg["momentum_regime"], MOM_REGIMES, "mom").items():
        add(k, v, "regime", "binary", False, "momentum regime one-hot")
    for k, v in _onehot(rg["participation_regime"], PART_REGIMES, "part").items():
        add(k, v, "regime", "binary", False, "participation one-hot (insufficient explicit)")
    for k, v in _onehot(rg["composite_regime"], COMPOSITES, "comp").items():
        add(k, v, "regime", "binary", False, "composite regime one-hot")

    frame = pd.DataFrame(cols, index=idx)
    required = [s.name for s in specs if not s.optional]
    finite_required = np.isfinite(frame[required].to_numpy(dtype=np.float64)).all(axis=1)
    known_state = (
        (pa["structure"].reindex(idx) != "insufficient_data")
        & (rg["volatility_regime"].reindex(idx) != "insufficient_data")
        & (rg["momentum_regime"].reindex(idx) != "insufficient_data")
    ).to_numpy()
    availability = np.where(finite_required & known_state, VALID, INSUFFICIENT)
    times = session_times(pd.DatetimeIndex(idx), calendar or TradingCalendar("XNYS"))
    frame.insert(0, "availability", availability)
    frame.insert(0, "observed_at", times["observed_at"])
    fp = hashlib.sha256(
        (
            "|".join(f"{s.name}:{s.kind}:{s.optional}:{s.definition}" for s in specs)
            + FEATURE_VERSION
        ).encode()
    ).hexdigest()[:16]
    return FeatureDataset(instrument_id, timeframe, FEATURE_VERSION, fp, tuple(specs), frame)


def build_features(bars: pd.DataFrame, *, instrument_id: int | None = None) -> FeatureDataset:
    return assemble(
        compute_components(bars), bars["close"].astype("float64"), instrument_id=instrument_id
    )
