"""RegimeEngine: closed OHLCV bars -> descriptive market-regime state per bar.

Composes existing engines instead of re-implementing them:
- trend      <- PriceActionEngine `structure` (confirmed pivots only)
- volatility <- IndicatorEngine `realized_vol_<n>` ranked causally against its own past
- momentum   <- IndicatorEngine `rsi_<n>`, `roc_<n>`, `macd`
- participation <- IndicatorEngine `relative_volume_<n>` (instrument-level, NOT breadth)

Regime labels describe observed market conditions; they are not trading signals or predictions.
"""

import hashlib
from dataclasses import dataclass

import pandas as pd

from app.indicators.config import IndicatorConfig
from app.indicators.engine import IndicatorEngine
from app.price_action.config import PriceActionConfig
from app.price_action.engine import PriceActionEngine
from app.regimes import regimes as rg
from app.regimes.config import ENGINE_VERSION, RegimeConfig

DIMENSIONS = ("trend", "volatility", "momentum", "participation", "composite")


@dataclass(frozen=True)
class RegimeAnalysis:
    state: pd.DataFrame  # one row per bar; available at that bar's close
    engine_version: str
    config_fingerprint: str  # regime + upstream price-action + indicator configurations

    def state_as_of(self, ts: pd.Timestamp) -> pd.Series:
        eligible = self.state.loc[: pd.Timestamp(ts)]
        if eligible.empty:
            raise LookupError(f"no regime state at or before {ts}")
        return eligible.iloc[-1]


class RegimeEngine:
    def __init__(
        self,
        config: RegimeConfig | None = None,
        *,
        price_action_config: PriceActionConfig | None = None,
        indicator_config: IndicatorConfig | None = None,
    ) -> None:
        self.config = config or RegimeConfig()
        self.price_action = PriceActionEngine(price_action_config)
        self.indicators = IndicatorEngine(indicator_config)

    def fingerprint(self) -> str:
        parts = (
            self.config.fingerprint(),
            self.price_action.config.fingerprint(),
            self.indicators.config.fingerprint(),
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def analyze(
        self, bars: pd.DataFrame, *, instrument_id: int | None = None, timeframe: str = "1d"
    ) -> RegimeAnalysis:
        cfg, icfg = self.config, self.indicators.config
        # The Indicator Engine validates inputs (UTC, order, closed, finite positive OHLC,
        # volume finite/non-negative or missing).
        ind = self.indicators.analyze(bars, instrument_id=instrument_id, timeframe=timeframe).values

        # The Price Action Engine requires a complete volume column although none of its outputs
        # depend on volume (tested). Missing volume is therefore passed to it as 0; that value
        # never reaches any regime output.
        pa_bars = bars.assign(volume=bars["volume"].fillna(0.0))
        pa = self.price_action.analyze(
            pa_bars, instrument_id=instrument_id, timeframe=timeframe
        ).state

        vol_measure = ind[f"realized_vol_{icfg.realized_vol_period}"]
        # Ranked on values rounded to 1e-10: floating-point noise (e.g. ~1e-17 "volatility" of
        # constant returns) must not be ranked as if it were a real difference.
        pct = rg.causal_percentile_rank(
            vol_measure.round(rg.ROUND), cfg.volatility_lookback, cfg.volatility_min_observations
        )
        rsi = ind[f"rsi_{icfg.rsi_period}"]
        roc = ind[f"roc_{icfg.roc_period}"]
        macd = ind["macd"]
        rel_vol = ind[f"relative_volume_{icfg.relative_volume_period}"]

        state = pd.DataFrame(
            {
                "instrument_id": instrument_id,
                "timeframe": timeframe,
                "trend_regime": pa["structure"].astype(object),
                "trend_quality": pa["trend_quality"],
                "volatility_measure": vol_measure,
                "volatility_percentile": pct["percentile"],
                "volatility_reference_count": pct["reference_count"],
                "volatility_regime": rg.volatility_regime(pct["percentile"], cfg),
                "atr_pct": (ind[f"atr_{icfg.atr_period}"] / bars["close"]).round(rg.ROUND),
                "momentum_rsi": rsi,
                "momentum_roc": roc,
                "momentum_macd": macd,
                "momentum_regime": rg.momentum_regime(rsi, roc, macd, cfg),
                "relative_volume": rel_vol,
                "participation_regime": rg.participation_state(rel_vol, cfg),
            },
            index=bars.index,
        )
        state["composite_regime"] = rg.composite_regime(
            state["trend_regime"], state["volatility_regime"]
        )
        for dim in DIMENSIONS:
            tr = rg.transitions(state[f"{dim}_regime"])
            state[f"{dim}_changed"] = tr["changed"]
            state[f"{dim}_age"] = tr["age"]
        return RegimeAnalysis(
            state=state, engine_version=ENGINE_VERSION, config_fingerprint=self.fingerprint()
        )
