"""Read-only loading of closed bars WITH the adjusted close, for the total-return benchmark.

The Phase 3 loader (raw OHLCV, used by every feature engine) is left unchanged; `adj_close` is
added here as a separate column and is used ONLY to value the total-return benchmark.
"""

from datetime import datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.candles.loader import load_closed_bars
from app.database.models import PriceBar


def load_backtest_bars(
    session: Session,
    symbol: str,
    *,
    provider: str = "yfinance",
    timeframe: str = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[int, pd.DataFrame]:
    instrument_id, bars = load_closed_bars(
        session, symbol, provider=provider, timeframe=timeframe, start=start, end=end
    )
    stmt = select(PriceBar.ts, PriceBar.adj_close).where(
        PriceBar.instrument_id == instrument_id,
        PriceBar.provider == provider,
        PriceBar.timeframe == timeframe,
        PriceBar.is_closed.is_(True),
    )
    adj = pd.DataFrame(session.execute(stmt).all(), columns=["ts", "adj_close"])
    adj["ts"] = pd.to_datetime(adj["ts"], utc=True)
    adj_series = adj.set_index("ts")["adj_close"].astype("float64")
    return instrument_id, bars.assign(adj_close=adj_series.reindex(bars.index))
