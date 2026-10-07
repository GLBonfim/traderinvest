"""CandlestickEngine: closed OHLCV bars -> geometry + pattern observations.

The engine makes NO trading decisions. Its outputs are descriptive observations intended as
features/hypotheses for later out-of-sample statistical validation.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from app.candles.config import ENGINE_VERSION, CandleConfig
from app.candles.geometry import OHLCV, compute_geometry
from app.candles.patterns import REGISTRY, PatternSpec

OBSERVATION_COLUMNS = (
    "ts",
    "instrument_id",
    "timeframe",
    "pattern",
    "family",
    "orientation",
    "bars_in_pattern",
    "first_bar_ts",
    "strength",
    "context_requirements_met",
    "prior_trend",
    "trend_score",
    "body_ratio",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "range",
    "relative_volume",
)


@dataclass(frozen=True)
class CandlestickObservation:
    """One pattern on one candle. `orientation` is a conventional label, not a prediction."""

    ts: datetime  # open time (UTC) of the LAST candle of the pattern
    instrument_id: int | None
    timeframe: str
    pattern: str
    family: str
    orientation: str
    bars_in_pattern: int
    first_bar_ts: datetime
    strength: float  # geometric quality in [0, 1]; no predictive meaning
    context_requirements_met: bool
    prior_trend: str  # up | down | none | unknown, measured before the first candle
    trend_score: float | None
    body_ratio: float
    upper_wick_ratio: float
    lower_wick_ratio: float
    range: float
    relative_volume: float | None


@dataclass(frozen=True)
class CandleAnalysis:
    geometry: pd.DataFrame  # one row per input bar
    observations: pd.DataFrame  # long format, OBSERVATION_COLUMNS, sorted (ts, pattern)
    engine_version: str
    config_fingerprint: str

    def to_observations(self) -> list[CandlestickObservation]:
        def opt(v: Any) -> float | None:
            return None if pd.isna(v) else float(v)

        records: list[dict[str, Any]] = self.observations.to_dict("records")  # type: ignore[assignment]
        return [
            CandlestickObservation(
                ts=r["ts"].to_pydatetime(),
                instrument_id=None if pd.isna(r["instrument_id"]) else int(r["instrument_id"]),
                timeframe=str(r["timeframe"]),
                pattern=str(r["pattern"]),
                family=str(r["family"]),
                orientation=str(r["orientation"]),
                bars_in_pattern=int(r["bars_in_pattern"]),
                first_bar_ts=r["first_bar_ts"].to_pydatetime(),
                strength=float(r["strength"]),
                context_requirements_met=bool(r["context_requirements_met"]),
                prior_trend=str(r["prior_trend"]),
                trend_score=opt(r["trend_score"]),
                body_ratio=float(r["body_ratio"]),
                upper_wick_ratio=float(r["upper_wick_ratio"]),
                lower_wick_ratio=float(r["lower_wick_ratio"]),
                range=float(r["range"]),
                relative_volume=opt(r["relative_volume"]),
            )
            for r in records
        ]


class CandlestickEngine:
    def __init__(self, config: CandleConfig | None = None) -> None:
        self.config = config or CandleConfig()

    @staticmethod
    def _check_input(bars: pd.DataFrame) -> None:
        missing = set(OHLCV) - set(bars.columns)
        if missing:
            raise ValueError(f"bars missing columns: {sorted(missing)}")
        idx = bars.index
        if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
            raise ValueError("bars must be indexed by a timezone-aware DatetimeIndex")
        if str(idx.tz) != "UTC":
            raise ValueError("bars index must be in UTC")
        if not idx.is_monotonic_increasing or not idx.is_unique:
            raise ValueError("bars index must be strictly increasing (sorted, no duplicates)")
        if "is_closed" in bars.columns and not bars["is_closed"].astype(bool).all():
            raise ValueError("engine accepts only closed bars; filter is_closed first")

    def analyze(
        self,
        bars: pd.DataFrame,
        *,
        instrument_id: int | None = None,
        timeframe: str = "1d",
    ) -> CandleAnalysis:
        """Bars must be consecutive closed bars of one instrument/timeframe, sorted, UTC."""
        self._check_input(bars)
        cfg = self.config
        g = compute_geometry(bars, cfg)

        frames: list[pd.DataFrame] = []
        for spec, detector in REGISTRY:
            mask, strength = detector(g, cfg)
            context = self._context(g, spec)
            if spec.trend_defines_identity:
                mask = mask & context
            mask = mask.fillna(False).astype(bool)
            if not mask.any():
                continue
            rows = g[mask]
            first_ts = pd.Series(g.index, index=g.index).shift(spec.bars - 1)[mask]
            trend_at_start = g["prior_trend"].shift(spec.bars - 1, fill_value="unknown")[mask]
            score_at_start = g["trend_score"].shift(spec.bars - 1)[mask]
            frames.append(
                pd.DataFrame(
                    {
                        "ts": rows.index,
                        "instrument_id": instrument_id,
                        "timeframe": timeframe,
                        "pattern": spec.name,
                        "family": spec.family,
                        "orientation": spec.orientation or rows["direction"].to_numpy(),
                        "bars_in_pattern": spec.bars,
                        "first_bar_ts": first_ts.to_numpy(),
                        "strength": strength[mask].round(6).to_numpy(),
                        "context_requirements_met": context[mask].to_numpy(),
                        "prior_trend": trend_at_start.to_numpy(),
                        "trend_score": score_at_start.to_numpy(),
                        "body_ratio": rows["body_ratio"].to_numpy(),
                        "upper_wick_ratio": rows["upper_wick_ratio"].to_numpy(),
                        "lower_wick_ratio": rows["lower_wick_ratio"].to_numpy(),
                        "range": rows["range"].to_numpy(),
                        "relative_volume": rows["relative_volume"].to_numpy(),
                    }
                )
            )

        if frames:
            observations = pd.concat(frames, ignore_index=True)
            observations = observations.sort_values(["ts", "pattern"], kind="stable")
            observations = observations.reset_index(drop=True)
        else:
            observations = pd.DataFrame(columns=list(OBSERVATION_COLUMNS))
        return CandleAnalysis(
            geometry=g,
            observations=observations[list(OBSERVATION_COLUMNS)],
            engine_version=ENGINE_VERSION,
            config_fingerprint=cfg.fingerprint(),
        )

    @staticmethod
    def _context(g: pd.DataFrame, spec: PatternSpec) -> pd.Series:
        """Prior trend is measured BEFORE the pattern's first candle (past-only)."""
        if spec.context_trend is None:
            return pd.Series(True, index=g.index)
        trend = g["prior_trend"].shift(spec.bars - 1)
        return (trend == spec.context_trend).fillna(False).astype(bool)
