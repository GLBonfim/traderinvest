"""Market data views: dataset identity, bars, overview, freshness. Reads the local database
only (never a data provider)."""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.backtest.data import load_backtest_bars
from app.dashboard.config import PROVIDER, TIMEFRAME
from app.data.calendar import TradingCalendar
from app.database.models import IngestionRun, Instrument, PriceBar


class NoDataError(LookupError):
    """The instrument or its bars are not available in the local database."""


@dataclass(frozen=True)
class DatasetIdentity:
    """Cheap fingerprint of the stored dataset; every cached computation is keyed by it."""

    symbol: str
    instrument_id: int
    provider: str
    timeframe: str
    bars: int
    first_ts: pd.Timestamp
    last_ts: pd.Timestamp
    last_ingested_at: pd.Timestamp

    @property
    def key(self) -> str:
        raw = "|".join(
            (
                __version__,
                self.symbol,
                str(self.instrument_id),
                self.provider,
                self.timeframe,
                str(self.bars),
                self.first_ts.isoformat(),
                self.last_ts.isoformat(),
                self.last_ingested_at.isoformat(),
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def dataset_identity(
    session: Session, symbol: str, provider: str = PROVIDER, timeframe: str = TIMEFRAME
) -> DatasetIdentity:
    instrument_id = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
    if instrument_id is None:
        raise NoDataError(f"instrument {symbol!r} not found in the local database")
    row = session.execute(
        select(
            func.count(PriceBar.id),
            func.min(PriceBar.ts),
            func.max(PriceBar.ts),
            func.max(PriceBar.ingested_at),
        ).where(
            PriceBar.instrument_id == instrument_id,
            PriceBar.provider == provider,
            PriceBar.timeframe == timeframe,
            PriceBar.is_closed.is_(True),
        )
    ).one()
    if not row[0]:
        raise NoDataError(f"no closed {timeframe} bars for {symbol!r} from {provider!r}")
    return DatasetIdentity(
        symbol,
        int(instrument_id),
        provider,
        timeframe,
        int(row[0]),
        pd.Timestamp(row[1]).tz_convert("UTC"),
        pd.Timestamp(row[2]).tz_convert("UTC"),
        pd.Timestamp(row[3]).tz_convert("UTC"),
    )


def load_bars(session: Session, ident: DatasetIdentity) -> pd.DataFrame:
    """Raw OHLCV + adj_close (adj_close only for the total-return benchmark, as in Phase 8)."""
    _, bars = load_backtest_bars(
        session, ident.symbol, provider=ident.provider, timeframe=ident.timeframe
    )
    if len(bars) != ident.bars:
        raise NoDataError("dataset changed while loading; reload the page")
    return bars


def latest_ingestion(session: Session, instrument_id: int) -> dict[str, Any] | None:
    run = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.instrument_id == instrument_id)
        .order_by(IngestionRun.id.desc())
        .limit(1)
    ).first()
    if run is None:
        return None
    return {
        "run_id": run.id,
        "provider": run.provider,
        "provider_version": run.provider_version,
        "status": run.status,
        "requested_start": run.requested_start,
        "requested_end": run.requested_end,
        "as_of": run.as_of,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "bars_inserted": run.bars_inserted,
        "bars_updated": run.bars_updated,
        "issues_count": run.issues_count,
        "error": run.error,
    }


@dataclass(frozen=True)
class Freshness:
    now: pd.Timestamp
    last_bar_session: date
    expected_session: date | None  # latest XNYS session whose close is <= now
    missing_sessions: int  # completed sessions after the last stored bar
    stale: bool
    market_status: str  # descriptive, from the exchange calendar only


def freshness(
    last_bar_ts: pd.Timestamp, now: datetime, calendar: TradingCalendar | None = None
) -> Freshness:
    """Compares the last stored bar with the exchange calendar at `now` (no market data)."""
    cal = calendar or TradingCalendar("XNYS")
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_ts = now_ts.tz_convert("UTC")
    last_date = last_bar_ts.tz_convert(cal.tz).date()
    sess = cal.sessions(last_date - timedelta(days=10), now_ts.date() + timedelta(days=1))
    completed = sess[sess["close_utc"] <= now_ts]
    days = pd.DatetimeIndex(sess.index).date
    done = pd.DatetimeIndex(completed.index).date
    expected = done[-1] if len(done) else None
    missing = int((done > last_date).sum())
    today = sess[days == now_ts.tz_convert(cal.tz).date()]
    if today.empty:
        status = "closed (no regular session today)"
    else:
        o, c = today["open_utc"].iloc[0], today["close_utc"].iloc[0]
        if now_ts < o:
            status = "closed (before today's regular session)"
        elif now_ts < c:
            status = "regular session in progress (today's bar is not complete)"
        else:
            status = "closed (today's regular session has ended)"
    return Freshness(now_ts, last_date, expected, missing, missing > 0, status)


def market_overview(bars: pd.DataFrame, calendar: TradingCalendar | None = None) -> dict[str, Any]:
    """Latest completed session values, straight from the stored raw bars."""
    if bars.empty:
        raise NoDataError("no bars")
    cal = calendar or TradingCalendar("XNYS")
    last, ts = bars.iloc[-1], bars.index[-1]
    prev_close = float(bars["close"].iloc[-2]) if len(bars) > 1 else float("nan")
    close = float(last["close"])
    return {
        "session": ts.tz_convert(cal.tz).date(),
        "bar_ts": ts,
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "close": close,
        "volume": float(last["volume"]),
        "previous_close": prev_close,
        "change": close - prev_close,
        "change_pct": close / prev_close - 1,
        "first_session": bars.index[0].tz_convert(cal.tz).date(),
        "bars": len(bars),
    }


def session_dates(bars: pd.DataFrame, tz: str = "America/New_York") -> list[date]:
    return [t.date() for t in pd.DatetimeIndex(bars.index).tz_convert(tz)]


def position(index: pd.Index, ts: pd.Timestamp) -> int:
    """Integer position of an exact, unique timestamp in an index."""
    pos = index.get_loc(ts)
    if not isinstance(pos, int):
        raise KeyError(f"{ts} is not a unique index entry")
    return pos


def session_on_or_before(sessions: list[date], chosen: date) -> date:
    """The latest stored session on or before `chosen` (stated explicitly in the UI)."""
    candidates = [s for s in sessions if s <= chosen]
    if not candidates:
        raise NoDataError(f"no stored session on or before {chosen}")
    return candidates[-1]


def bar_ts_for(bars: pd.DataFrame, session: date, tz: str = "America/New_York") -> pd.Timestamp:
    """The stored bar of a session date (exact match only; no nearest-date guessing)."""
    idx = pd.DatetimeIndex(bars.index)
    hits = idx[idx.tz_convert(tz).date == session]
    if len(hits) != 1:
        raise NoDataError(f"no stored bar for session {session}")
    return pd.Timestamp(hits[0])
