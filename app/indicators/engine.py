"""IndicatorEngine: closed OHLCV bars -> technical indicator values (descriptive features).

Indicator values are observations. Nothing here produces trading decisions.

Price convention: every indicator uses the provider's RAW OHLC (`open/high/low/close`) and raw
volume, the same series as the Candlestick and Price Action engines. `adj_close` is not used
(see ADR-0014). Volume may be missing: volume-based indicators are then NaN; nothing is invented.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.indicators import indicators as ind
from app.indicators.config import ENGINE_VERSION, IndicatorConfig

PRICE_COLUMNS = ("open", "high", "low", "close")
CATALOG_COLUMNS = ("column", "family", "inputs", "definition", "first_valid_index")


@dataclass(frozen=True)
class IndicatorAnalysis:
    values: pd.DataFrame  # one row per bar; metadata + indicator columns
    catalog: pd.DataFrame  # per indicator column: family, inputs, definition, warm-up
    engine_version: str
    config_fingerprint: str

    @property
    def indicator_columns(self) -> list[str]:
        return list(self.catalog["column"])

    def value_as_of(self, ts: pd.Timestamp) -> pd.Series:
        """Values after the last bar whose ts <= `ts` closed."""
        eligible = self.values.loc[: pd.Timestamp(ts)]
        if eligible.empty:
            raise LookupError(f"no indicator values at or before {ts}")
        return eligible.iloc[-1]


class IndicatorEngine:
    def __init__(self, config: IndicatorConfig | None = None) -> None:
        self.config = config or IndicatorConfig()

    @staticmethod
    def _check_input(bars: pd.DataFrame) -> None:
        missing = {*PRICE_COLUMNS, "volume"} - set(bars.columns)
        if missing:
            raise ValueError(f"bars missing columns: {sorted(missing)}")
        idx = bars.index
        if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None or str(idx.tz) != "UTC":
            raise ValueError("bars must be indexed by a UTC DatetimeIndex")
        if not idx.is_monotonic_increasing or not idx.is_unique:
            raise ValueError("bars index must be strictly increasing (sorted, no duplicates)")
        if "is_closed" in bars.columns and not bars["is_closed"].astype(bool).all():
            raise ValueError("engine accepts only closed bars; filter is_closed first")
        prices = bars[list(PRICE_COLUMNS)].to_numpy(dtype=np.float64)
        if not np.isfinite(prices).all():
            raise ValueError("bars contain missing or infinite OHLC values; validate data first")
        if (prices <= 0).any():
            raise ValueError("bars contain non-positive prices")
        volume = bars["volume"].to_numpy(dtype=np.float64)
        if np.isinf(volume).any() or (volume < 0).any():
            raise ValueError("volume must be finite and >= 0 (missing volume is allowed as NaN)")

    def analyze(
        self, bars: pd.DataFrame, *, instrument_id: int | None = None, timeframe: str = "1d"
    ) -> IndicatorAnalysis:
        self._check_input(bars)
        cfg = self.config
        # Work on float copies: the caller's frame is never modified.
        high, low, close = (bars[c].astype("float64").copy() for c in ("high", "low", "close"))
        volume = bars["volume"].astype("float64").copy()

        cols: dict[str, pd.Series] = {}
        catalog: list[tuple[str, str, str, str, int]] = []

        def add(
            name: str, series: pd.Series, family: str, inputs: str, definition: str, warm: int
        ) -> None:
            cols[name] = series
            catalog.append((name, family, inputs, definition, warm))

        for n in cfg.sma_periods:
            add(f"sma_{n}", ind.sma(close, n), "trend", "close", f"mean(close, last {n})", n - 1)
        for n in cfg.ema_periods:
            add(
                f"ema_{n}",
                ind.ema(close, n),
                "trend",
                "close",
                f"EMA alpha=2/({n}+1), SMA seed",
                n - 1,
            )

        m = ind.macd(close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
        macd_start = cfg.macd_slow - 1
        sig_start = macd_start + cfg.macd_signal - 1
        add("macd", m["macd"], "trend", "close",
            f"EMA{cfg.macd_fast} - EMA{cfg.macd_slow}", macd_start)  # fmt: skip
        add(
            "macd_signal",
            m["macd_signal"],
            "trend",
            "macd",
            f"EMA{cfg.macd_signal}(macd)",
            sig_start,
        )
        add("macd_hist", m["macd_hist"], "trend", "macd", "macd - macd_signal", sig_start)

        add(f"rsi_{cfg.rsi_period}", ind.rsi(close, cfg.rsi_period), "momentum", "close",
            f"Wilder RSI({cfg.rsi_period})", cfg.rsi_period)  # fmt: skip

        st = ind.stochastic(
            high, low, close, cfg.stoch_period, cfg.stoch_k_smoothing, cfg.stoch_d_period
        )
        raw_start = cfg.stoch_period - 1
        k_start = raw_start + cfg.stoch_k_smoothing - 1
        add("stoch_raw_k", st["stoch_raw_k"], "momentum", "high,low,close",
            f"100 (close - LL{cfg.stoch_period}) / (HH - LL)", raw_start)  # fmt: skip
        add("stoch_k", st["stoch_k"], "momentum", "stoch_raw_k",
            f"SMA{cfg.stoch_k_smoothing}(raw %K)", k_start)  # fmt: skip
        add("stoch_d", st["stoch_d"], "momentum", "stoch_k",
            f"SMA{cfg.stoch_d_period}(%K)", k_start + cfg.stoch_d_period - 1)  # fmt: skip

        add(f"roc_{cfg.roc_period}", ind.roc(close, cfg.roc_period), "momentum", "close",
            f"100 (close / close[t-{cfg.roc_period}] - 1)", cfg.roc_period)  # fmt: skip

        tr = ind.true_range(high, low, close)
        add("true_range", tr, "volatility", "high,low,close", "max(H-L, |H-Cp|, |L-Cp|)", 1)
        add(f"atr_{cfg.atr_period}", ind.wilder(tr, cfg.atr_period), "volatility", "true_range",
            f"Wilder({cfg.atr_period}) of true range", cfg.atr_period)  # fmt: skip

        bb = ind.bollinger(close, cfg.bollinger_period, cfg.bollinger_k, cfg.bollinger_ddof)
        bb_def = f"SMA{cfg.bollinger_period} +/- {cfg.bollinger_k} std(ddof={cfg.bollinger_ddof})"
        for name in bb.columns:
            add(name, bb[name], "volatility", "close", bb_def, cfg.bollinger_period - 1)

        add(f"realized_vol_{cfg.realized_vol_period}",
            ind.realized_volatility(close, cfg.realized_vol_period, cfg.realized_vol_ddof,
                                    cfg.trading_days_per_year),
            "volatility", "close",
            f"std(log returns, last {cfg.realized_vol_period}, ddof={cfg.realized_vol_ddof})"
            f" x sqrt({cfg.trading_days_per_year})",
            cfg.realized_vol_period)  # fmt: skip

        add(f"relative_volume_{cfg.relative_volume_period}",
            ind.relative_volume(volume, cfg.relative_volume_period), "volume", "volume",
            f"volume / mean(previous {cfg.relative_volume_period} volumes)",
            cfg.relative_volume_period)  # fmt: skip
        add("obv", ind.obv(close, volume), "volume", "close,volume",
            "cumsum(sign(close change) x volume), OBV[0] = 0", 0)  # fmt: skip

        values = pd.DataFrame(cols, index=bars.index)
        values.insert(0, "timeframe", timeframe)
        values.insert(0, "instrument_id", instrument_id)
        return IndicatorAnalysis(
            values=values,
            catalog=pd.DataFrame(catalog, columns=list(CATALOG_COLUMNS)),
            engine_version=ENGINE_VERSION,
            config_fingerprint=cfg.fingerprint(),
        )
