"""Synthetic OHLCV builders for Candlestick Engine tests. NOT market data."""

from collections.abc import Sequence

import pandas as pd

from app.candles.engine import CandleAnalysis, CandlestickEngine

# (open, high, low, close) ; volume defaults to 1_000_000
Candle = tuple[float, float, float, float]


def bars(candles: Sequence[Candle], volumes: Sequence[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 14:30", periods=len(candles), freq="D", tz="UTC", name="ts")
    vols = list(volumes) if volumes is not None else [1_000_000.0] * len(candles)
    return pd.DataFrame(
        {
            "open": [c[0] for c in candles],
            "high": [c[1] for c in candles],
            "low": [c[2] for c in candles],
            "close": [c[3] for c in candles],
            "volume": vols,
        },
        index=idx,
    ).astype("float64")


def trend_prefix(direction: str, end_close: float, n: int = 12) -> list[Candle]:
    """n candles stepping 1.0 per bar towards `end_close` (trend_score ~ 5 with range 2).

    direction: "down" | "up" | "flat". Range 2.0; body 1.0 (flat: body 0.5).
    """
    out: list[Candle] = []
    for i in range(n):
        steps_left = n - 1 - i
        if direction == "down":
            close = end_close + steps_left
            open_ = close + 1
        elif direction == "up":
            close = end_close - steps_left
            open_ = close - 1
        else:  # alternating, no net move
            close = end_close
            open_ = end_close + (0.5 if i % 2 else -0.5)
        mid = (open_ + close) / 2
        out.append((open_, mid + 1, mid - 1, close))
    return out


def analyze(candles: Sequence[Candle], **kw: object) -> CandleAnalysis:
    return CandlestickEngine().analyze(bars(candles), **kw)  # type: ignore[arg-type]


def patterns_at_last(candles: Sequence[Candle]) -> set[str]:
    a = analyze(candles)
    last = a.geometry.index[-1]
    return set(a.observations.loc[a.observations["ts"] == last, "pattern"])


def observation(candles: Sequence[Candle], pattern: str) -> pd.Series:
    a = analyze(candles)
    obs = a.observations
    hit = obs[(obs["ts"] == a.geometry.index[-1]) & (obs["pattern"] == pattern)]
    assert len(hit) == 1, f"{pattern} not detected on last candle"
    return hit.iloc[0]
