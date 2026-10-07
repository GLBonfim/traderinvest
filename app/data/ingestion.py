"""Daily-bar ingestion: fetch -> normalize -> validate -> persist (idempotent) -> record issues.

Idempotency: bars are keyed by (instrument, provider, timeframe, ts). Re-running with the same
provider data inserts nothing and updates nothing. Changes are applied but never silently:
- a bar that was still forming (is_closed=false) is updated without an event (expected);
- adj_close-only changes on closed bars are expected after dividends: updated, counted, and
  summarised in one info event;
- any change to raw OHLCV of a CLOSED bar is a revision: updated and recorded as a warning with
  old and new values.
Every run is recorded in `ingestion_runs`; its events reference the run.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.data.calendar import TradingCalendar
from app.data.instruments import InstrumentSpec, ensure_instrument
from app.data.normalization import normalize_daily
from app.data.providers.base import DataProvider, ProviderError
from app.data.quality import ABORTED, EXCLUDED, UPDATED, DataQualityIssue
from app.data.validation import ValidationConfig, validate_daily
from app.database.models import DataQualityEvent, IngestionRun, MarketSession, PriceBar

log = get_logger(__name__)

TIMEFRAME = "1d"
_PRICE_Q = Decimal("0.000001")
_VOLUME_Q = Decimal("0.00000001")
_OHLCV = ("open", "high", "low", "close", "volume")
# Yahoo recomputes adj_close on every request; repeated fetches differ by ~1e-6 relative
# (floating-point noise, observed 2026-10-07). A real dividend restatement on SPY is ~1e-3.
# Differences below this tolerance keep the stored value and are only counted in the run log.
ADJ_CLOSE_REL_TOLERANCE = Decimal("0.00001")


@dataclass
class IngestionResult:
    run_id: int
    status: str
    bars_received: int = 0
    bars_inserted: int = 0
    bars_updated: int = 0
    bars_unchanged: int = 0
    bars_excluded: int = 0
    adj_close_noise_ignored: int = 0
    sessions_upserted: int = 0
    issues: list[DataQualityIssue] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        by_check: dict[str, int] = {}
        for i in self.issues:
            by_check[i.check_name] = by_check.get(i.check_name, 0) + 1
        return {
            "run_id": self.run_id,
            "status": self.status,
            "bars_received": self.bars_received,
            "bars_inserted": self.bars_inserted,
            "bars_updated": self.bars_updated,
            "bars_unchanged": self.bars_unchanged,
            "bars_excluded": self.bars_excluded,
            "adj_close_noise_ignored": self.adj_close_noise_ignored,
            "sessions_upserted": self.sessions_upserted,
            "issues": by_check,
            "error": self.error,
        }


def _dec(value: float, q: Decimal) -> Decimal | None:
    if pd.isna(value):
        return None
    return Decimal(repr(float(value))).quantize(q)


def _row_values(row: pd.Series) -> dict[str, Any]:
    return {
        "open": _dec(row["open"], _PRICE_Q),
        "high": _dec(row["high"], _PRICE_Q),
        "low": _dec(row["low"], _PRICE_Q),
        "close": _dec(row["close"], _PRICE_Q),
        "adj_close": _dec(row["adj_close"], _PRICE_Q),
        "volume": _dec(row["volume"], _VOLUME_Q),
        "is_closed": bool(row["is_closed"]),
    }


def _adj_differs(old: Decimal | None, new: Decimal | None) -> bool:
    if old is None or new is None:
        return old is not new
    if old == 0:
        return new != 0
    return abs(new / old - 1) > ADJ_CLOSE_REL_TOLERANCE


def upsert_sessions(session: Session, calendar: TradingCalendar, sessions: pd.DataFrame) -> int:
    if sessions.empty:
        return 0
    rows = [
        {
            "exchange": calendar.exchange,
            "session_date": d.date(),
            "market_open": o.to_pydatetime(),
            "market_close": c.to_pydatetime(),
            "is_early_close": bool(early),
            "source": calendar.source,
        }
        for d, o, c, early in zip(
            sessions.index,
            sessions["open_utc"],
            sessions["close_utc"],
            sessions["is_early_close"],
            strict=True,
        )
    ]
    stmt = insert(MarketSession)
    stmt = stmt.on_conflict_do_update(
        index_elements=["exchange", "session_date"],
        set_={
            "market_open": stmt.excluded.market_open,
            "market_close": stmt.excluded.market_close,
            "is_early_close": stmt.excluded.is_early_close,
            "source": stmt.excluded.source,
        },
        where=tuple_(
            MarketSession.market_open, MarketSession.market_close, MarketSession.is_early_close
        ).is_distinct_from(
            tuple_(
                stmt.excluded.market_open,
                stmt.excluded.market_close,
                stmt.excluded.is_early_close,
            )
        ),
    )
    changed = 0
    for chunk_start in range(0, len(rows), 2000):
        result = session.execute(
            stmt.returning(MarketSession.id), rows[chunk_start : chunk_start + 2000]
        )
        changed += len(result.all())
    return changed


def _persist_bars(
    session: Session,
    instrument_id: int,
    provider: str,
    valid: pd.DataFrame,
    result: IngestionResult,
) -> list[DataQualityIssue]:
    issues: list[DataQualityIssue] = []
    if valid.empty:
        return issues

    existing = {
        bar.ts: bar
        for bar in session.scalars(
            select(PriceBar).where(
                PriceBar.instrument_id == instrument_id,
                PriceBar.provider == provider,
                PriceBar.timeframe == TIMEFRAME,
                PriceBar.ts >= valid.index.min().to_pydatetime(),
                PriceBar.ts <= valid.index.max().to_pydatetime(),
            )
        )
    }

    to_insert: list[dict[str, Any]] = []
    adj_restated: list[tuple[datetime, Decimal]] = []  # (ts, relative change)
    for ts, (_, row) in zip(valid.index, valid.iterrows(), strict=True):
        ts_dt = ts.to_pydatetime()
        new = _row_values(row)
        old = existing.get(ts_dt)
        if old is None:
            to_insert.append(
                {
                    "instrument_id": instrument_id,
                    "provider": provider,
                    "timeframe": TIMEFRAME,
                    "ts": ts_dt,
                    **new,
                }
            )
            continue

        raw_changed = [c for c in _OHLCV if getattr(old, c) != new[c]]
        if (old.adj_close is None) != (new["adj_close"] is None):
            raw_changed.append("adj_close")  # appearing/disappearing is a revision, not noise
        adj_changed = _adj_differs(old.adj_close, new["adj_close"])
        closed_changed = old.is_closed != new["is_closed"]
        if not (raw_changed or adj_changed or closed_changed):
            if old.adj_close != new["adj_close"]:
                result.adj_close_noise_ignored += 1
            result.bars_unchanged += 1
            continue

        if old.is_closed and raw_changed:
            issues.append(
                DataQualityIssue(
                    check_name="bar_revised",
                    severity="warning",
                    description="Provider changed raw OHLCV of a closed bar; new values stored.",
                    action_taken=UPDATED,
                    bar_ts=ts_dt,
                    details={
                        c: {"old": str(getattr(old, c)), "new": str(new[c])} for c in raw_changed
                    },
                )
            )
        elif (
            old.is_closed
            and adj_changed
            and not raw_changed
            and old.adj_close
            and new["adj_close"] is not None
        ):
            adj_restated.append((ts_dt, abs(new["adj_close"] / old.adj_close - 1)))

        for col, value in new.items():
            setattr(old, col, value)
        result.bars_updated += 1

    if to_insert:
        stmt = insert(PriceBar).on_conflict_do_nothing(
            index_elements=["instrument_id", "provider", "timeframe", "ts"]
        )
        for i in range(0, len(to_insert), 2000):
            session.execute(stmt, to_insert[i : i + 2000])
        result.bars_inserted += len(to_insert)

    if adj_restated:
        issues.append(
            DataQualityIssue(
                check_name="adj_close_restated",
                severity="info",
                description="Provider restated adj_close of closed bars (expected after "
                "dividends).",
                action_taken=UPDATED,
                details={
                    "bars": len(adj_restated),
                    "first_ts": min(t for t, _ in adj_restated).isoformat(),
                    "last_ts": max(t for t, _ in adj_restated).isoformat(),
                    "max_rel_change": float(max(r for _, r in adj_restated)),
                },
            )
        )
    session.flush()
    return issues


def _record_issues(
    session: Session,
    run: IngestionRun,
    instrument_id: int,
    issues: list[DataQualityIssue],
) -> None:
    session.add_all(
        DataQualityEvent(
            ingestion_run_id=run.id,
            provider=run.provider,
            instrument_id=instrument_id,
            timeframe=run.timeframe,
            bar_ts=i.bar_ts,
            check_name=i.check_name,
            severity=i.severity,
            description=i.description,
            action_taken=i.action_taken,
            details=i.details or None,
        )
        for i in issues
    )


def ingest_daily_bars(
    session: Session,
    provider: DataProvider,
    spec: InstrumentSpec,
    start: date,
    end: date,
    *,
    calendar: TradingCalendar | None = None,
    as_of: datetime | None = None,
    config: ValidationConfig | None = None,
) -> IngestionResult:
    """Run one ingestion. Commits its own transactions; the run row survives failures."""
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if start > end:
        raise ValueError("start must be <= end")
    calendar = calendar or TradingCalendar(spec.calendar)
    provider_symbol = spec.provider_symbols.get(provider.name)
    if provider_symbol is None:
        raise KeyError(f"{spec.symbol} has no symbol mapping for provider {provider.name!r}")

    instrument = ensure_instrument(session, spec)
    run = IngestionRun(
        provider=provider.name,
        instrument_id=instrument.id,
        timeframe=TIMEFRAME,
        requested_start=start,
        requested_end=end,
        as_of=as_of,
        status="running",
    )
    session.add(run)
    session.commit()
    result = IngestionResult(run_id=run.id, status="running")
    log.info(
        "ingestion.started",
        run_id=run.id,
        provider=provider.name,
        symbol=spec.symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        as_of=as_of.isoformat(),
    )

    try:
        bars = provider.fetch_bars(provider_symbol, TIMEFRAME, start, end)
        run.provider_version = bars.provider_version
        result.bars_received = len(bars.frame)

        normalized = normalize_daily(bars, calendar)
        expected_sessions = calendar.sessions(start, end)
        validation = validate_daily(
            normalized.frame, expected_sessions, as_of=as_of, requested_end=end, config=config
        )
        result.bars_excluded = validation.excluded_count + sum(
            1 for i in normalized.issues if i.action_taken == EXCLUDED
        )
        result.sessions_upserted = upsert_sessions(session, calendar, expected_sessions)
        persist_issues = _persist_bars(
            session, instrument.id, provider.name, validation.valid, result
        )
        result.issues = [*normalized.issues, *validation.issues, *persist_issues]
        result.status = "succeeded"
    except ProviderError as exc:
        session.rollback()
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        result.issues = [
            DataQualityIssue(
                check_name="provider_failure",
                severity="critical",
                description="Provider request failed; no data ingested.",
                action_taken=ABORTED,
                details={"error": result.error},
            )
        ]
    except Exception as exc:
        session.rollback()
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(UTC)
        session.commit()
        log.exception("ingestion.crashed", run_id=run.id)
        raise

    _record_issues(session, run, instrument.id, result.issues)
    for attr in (
        "bars_received",
        "bars_inserted",
        "bars_updated",
        "bars_unchanged",
        "bars_excluded",
    ):
        setattr(run, attr, getattr(result, attr))
    run.issues_count = len(result.issues)
    run.status = result.status
    run.error = result.error
    run.finished_at = datetime.now(UTC)
    session.commit()

    log.info("ingestion.finished", **result.summary())
    return result
