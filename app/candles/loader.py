"""Read closed bars from `price_bars` for the Candlestick Engine (read-only).

Candle geometry uses the provider's raw OHLC (open/high/low/close). `adj_close` is not used:
adjusting only the close would distort body and wick geometry. See docs/candlestick-engine.md.
"""

from datetime import datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Instrument, PriceBar


def load_closed_bars(
    session: Session,
    symbol: str,
    *,
    provider: str = "yfinance",
    timeframe: str = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[int, pd.DataFrame]:
    """Return (instrument_id, bars) with a UTC DatetimeIndex `ts` and float64 OHLCV columns."""
    instrument_id = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
    if instrument_id is None:
        raise LookupError(f"instrument {symbol!r} not found")

    stmt = (
        select(
            PriceBar.ts,
            PriceBar.open,
            PriceBar.high,
            PriceBar.low,
            PriceBar.close,
            PriceBar.volume,
        )
        .where(
            PriceBar.instrument_id == instrument_id,
            PriceBar.provider == provider,
            PriceBar.timeframe == timeframe,
            PriceBar.is_closed.is_(True),
        )
        .order_by(PriceBar.ts)
    )
    if start is not None:
        stmt = stmt.where(PriceBar.ts >= start)
    if end is not None:
        stmt = stmt.where(PriceBar.ts <= end)

    rows = session.execute(stmt).all()
    frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame = frame.set_index("ts")
    return instrument_id, frame.astype("float64")
