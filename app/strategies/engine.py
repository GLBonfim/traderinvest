"""StrategyEngine: closed bars -> features (from existing engines) -> baseline states.

Timing convention (ADR-0016), daily bars:
    bar_ts       session open of bar T (Phase 2 convention; row index)
    observed_at  session CLOSE of T: the earliest moment every feature of T is known and the
                 state is decided (= signal timestamp)
    effective_at OPEN of the next exchange session (from the XNYS calendar, known in advance):
                 the earliest moment the state may apply
No price is attached: how a state becomes a hypothetical fill is the backtester's job (Phase 8).

Baseline strategies are research benchmarks, not evidence of profitability.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from app.data.calendar import TradingCalendar
from app.indicators.config import IndicatorConfig
from app.indicators.engine import IndicatorEngine
from app.price_action.engine import PriceActionEngine
from app.regimes.engine import RegimeEngine
from app.strategies.base import INSUFFICIENT_DATA, STATES, Strategy
from app.strategies.baselines import baseline_strategies
from app.strategies.config import ENGINE_VERSION, StrategyConfig

SIGNAL_COLUMNS = (
    "strategy_id",
    "strategy_version",
    "instrument_id",
    "timeframe",
    "bar_ts",
    "observed_at",
    "effective_at",
    "state",
    "changed",
    "state_in_effect",
    "reason",
    "config_fingerprint",
)


@dataclass(frozen=True)
class StrategyRun:
    signals: pd.DataFrame  # long format: one row per (strategy, bar)
    summary: pd.DataFrame  # one row per strategy: state counts, changes, warm-up
    features: pd.DataFrame  # the feature frame the rules were applied to
    engine_version: str
    config_fingerprint: str

    def states(self, strategy_id: str) -> pd.Series:
        s = self.signals[self.signals["strategy_id"] == strategy_id]
        return pd.Series(
            s["state"].to_numpy(), index=pd.DatetimeIndex(s["bar_ts"]), name=strategy_id
        )


class StrategyEngine:
    def __init__(
        self,
        config: StrategyConfig | None = None,
        *,
        indicator_engine: IndicatorEngine | None = None,
        price_action_engine: PriceActionEngine | None = None,
        regime_engine: RegimeEngine | None = None,
        strategies: tuple[Strategy, ...] | None = None,
    ) -> None:
        self.config = cfg = config or StrategyConfig()
        periods = tuple(
            sorted({cfg.sma_trend_period, cfg.crossover_fast_period, cfg.crossover_slow_period})
        )
        self.indicators = indicator_engine or IndicatorEngine(
            IndicatorConfig(sma_periods=periods, rsi_period=cfg.rsi_period)
        )
        self.price_action = price_action_engine or PriceActionEngine()
        self.regimes = regime_engine or RegimeEngine()
        self.strategies = strategies or baseline_strategies(cfg)
        ids = [s.strategy_id for s in self.strategies]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate strategy ids")
        self.calendar = TradingCalendar(cfg.calendar)

    def fingerprint(self) -> str:
        parts = [
            self.config.fingerprint(),
            self.indicators.config.fingerprint(),
            self.price_action.config.fingerprint(),
            self.regimes.fingerprint(),
            *(f"{s.strategy_id}:{s.version}:{','.join(s.features)}" for s in self.strategies),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    # ── features ──

    def build_features(self, bars: pd.DataFrame, needed: set[str]) -> pd.DataFrame:
        """Only the engines required by `needed` are run. Raw close is the price feature."""
        out = pd.DataFrame({"close": bars["close"].astype("float64")}, index=bars.index)
        indicator_cols = {c for c in needed if c.startswith(("sma_", "rsi_"))}
        if indicator_cols:
            values = self.indicators.analyze(bars).values
            missing = indicator_cols - set(values.columns)
            if missing:
                raise ValueError(f"indicator engine does not produce {sorted(missing)}")
            for col in sorted(indicator_cols):
                out[col] = values[col]
        if "pa_structure" in needed:
            # None of the Price Action outputs depend on volume (tested in Phase 6); missing
            # volume is passed as 0 and never reaches a strategy.
            pa_bars = bars.assign(volume=bars["volume"].fillna(0.0))
            out["pa_structure"] = (
                self.price_action.analyze(pa_bars).state["structure"].astype(object)
            )
        if "regime_composite" in needed:
            out["regime_composite"] = self.regimes.analyze(bars).state["composite_regime"]
        return out

    # ── timestamps ──

    def session_times(self, index: pd.DatetimeIndex) -> pd.DataFrame:
        """observed_at = session close of each bar; effective_at = next session open."""
        if len(index) == 0:
            return pd.DataFrame({"observed_at": [], "effective_at": []}, index=index)
        first, last = index[0].date(), index[-1].date()
        sessions = self.calendar.sessions(first, last + timedelta(days=15))
        opens = pd.DatetimeIndex(sessions["open_utc"])
        pos = opens.get_indexer(index)
        if (pos < 0).any():
            bad = index[pos < 0][0]
            raise ValueError(f"bar ts {bad} is not a {self.calendar.exchange} session open")
        if (pos + 1 >= len(opens)).any():
            raise ValueError("calendar does not extend past the last bar")
        return pd.DataFrame(
            {
                "observed_at": sessions["close_utc"].to_numpy()[pos],
                "effective_at": opens[pos + 1],
            },
            index=index,
        )

    # ── run ──

    def run(
        self, bars: pd.DataFrame, *, instrument_id: int | None = None, timeframe: str = "1d"
    ) -> StrategyRun:
        if timeframe != "1d":
            raise ValueError("Phase 7 strategies support daily bars only")
        if not isinstance(bars.index, pd.DatetimeIndex) or str(bars.index.tz) != "UTC":
            raise ValueError("bars must be indexed by a UTC DatetimeIndex")
        if "is_closed" in bars.columns and not bars["is_closed"].astype(bool).all():
            raise ValueError("strategies accept only closed bars")
        needed = {f for s in self.strategies for f in s.features}
        features = self.build_features(bars, needed)
        times = self.session_times(pd.DatetimeIndex(bars.index))
        fingerprint = self.fingerprint()

        frames, summary = [], []
        for strategy in self.strategies:
            cols = list(strategy.features)
            state = strategy.decide(features[cols].copy())  # ONLY declared columns
            if not state.index.equals(features.index) or not set(state.unique()) <= set(STATES):
                raise ValueError(f"{strategy.strategy_id} returned an invalid state series")
            previous = state.shift(1)
            changed = state.ne(previous)
            changed.iloc[:1] = False
            in_effect = previous.fillna(INSUFFICIENT_DATA)  # decided at T-1, applies during T
            reason = _reasons(features[cols])
            frames.append(
                pd.DataFrame(
                    {
                        "strategy_id": strategy.strategy_id,
                        "strategy_version": strategy.version,
                        "instrument_id": instrument_id,
                        "timeframe": timeframe,
                        "bar_ts": features.index,
                        "observed_at": times["observed_at"].to_numpy(),
                        "effective_at": times["effective_at"].to_numpy(),
                        "state": state.to_numpy(),
                        "changed": changed.to_numpy(bool),
                        "state_in_effect": in_effect.to_numpy(),
                        "reason": reason,
                        "config_fingerprint": fingerprint,
                    }
                )
            )
            decided = state != INSUFFICIENT_DATA
            summary.append(
                {
                    "strategy_id": strategy.strategy_id,
                    "features": ",".join(cols),
                    "bars": len(state),
                    **{f"n_{s.lower()}": int((state == s).sum()) for s in STATES},
                    "state_changes": int(changed.sum()),
                    "first_decision_bar": int(decided.to_numpy().argmax())
                    if decided.any()
                    else None,
                }
            )
        signals = (
            pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SIGNAL_COLUMNS)
        )
        return StrategyRun(
            signals=signals[list(SIGNAL_COLUMNS)],
            summary=pd.DataFrame(summary),
            features=features,
            engine_version=ENGINE_VERSION,
            config_fingerprint=fingerprint,
        )


def _reasons(frame: pd.DataFrame) -> list[str]:
    """Human-readable record of the exact feature values the rule saw (no interpretation)."""
    parts = []
    for col in frame.columns:
        values = frame[col]
        if pd.api.types.is_float_dtype(values):
            parts.append([f"{col}={'NaN' if pd.isna(v) else f'{v:.6f}'}" for v in values])
        else:
            parts.append([f"{col}={v}" for v in values])
    return [";".join(row) for row in zip(*parts, strict=True)]
