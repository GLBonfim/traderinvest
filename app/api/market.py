"""Read-only market analysis endpoints for the web UI (ADR-0028).

GET /api/v1/market/summary          latest closed session: bar, regimes, indicators, candle
                                    observations and price-action structure
GET /api/v1/market/series?range=1y  closed bars plus chart indicators for 3m/6m/1y/2y/5y

SPY only, no symbol or as-of parameter. Every value comes from the existing engines (candles,
price action, indicators, regimes) run on the stored CLOSED daily bars, selected through the
dashboard services; nothing is re-implemented here. Database access is a single READ ONLY
transaction per request; nothing is written anywhere. Engine outputs are cached per dataset
identity (which changes with every ingestion), so expensive engines run once per dataset.
"""

import math
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, Protocol, TypeVar, cast

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.candles.engine import CandleAnalysis, CandlestickEngine
from app.core.logging import get_logger
from app.dashboard.services import analysis, market
from app.database.session import get_engine
from app.indicators.engine import IndicatorAnalysis, IndicatorEngine
from app.price_action.engine import PriceActionAnalysis, PriceActionEngine
from app.regimes.engine import RegimeAnalysis, RegimeEngine

router = APIRouter(prefix="/api/v1/market", tags=["market"])
log = get_logger(__name__)

SYMBOL = "SPY"
SOURCE = "LOCAL"
LABEL = "NOT LIVE · END-OF-DAY RESEARCH DATA"
PRICES = "raw, dividend-unadjusted"
SESSION_TZ = "America/New_York"
INSUFFICIENT = "insufficient_data"
# Calendar-free session counts (252 sessions ~ one year), so a range never depends on "today".
RANGES: dict[str, int] = {"3m": 63, "6m": 126, "1y": 252, "2y": 504, "5y": 1260}
RangeKey = Literal["3m", "6m", "1y", "2y", "5y"]
OVERLAYS = ("sma_20", "sma_50", "sma_200", "bb_upper", "bb_middle", "bb_lower")
OSCILLATORS = ("rsi_14", "macd", "macd_signal", "macd_hist")


# ── value cleaning ──


def num(v: Any) -> float | None:
    """Finite number or None: NaN, +/-inf and missing values are never turned into numbers."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def session_of(ts: pd.Timestamp) -> date:
    """XNYS session date of a bar (bars are stamped with the session open in UTC)."""
    return pd.Timestamp(ts).tz_convert(SESSION_TZ).date()


def jsonable(v: Any) -> Any:
    """Engine value -> JSON-safe value (timestamps as XNYS session dates, NaN/inf as None)."""
    v = analysis._clean(v)
    if isinstance(v, pd.Timestamp):
        return session_of(v).isoformat()
    if isinstance(v, bool | str) or v is None:
        return v
    if isinstance(v, int | float | np.number):
        return num(v)
    return str(v)


def series(values: pd.Series) -> list[float | None]:
    return [num(v) for v in values.to_numpy()]


# ── engines and cache ──


@dataclass(frozen=True)
class MarketEngines:
    """Engine outputs needed by the market views (strategies are deliberately not run)."""

    bars: pd.DataFrame
    candles: CandleAnalysis
    price_action: PriceActionAnalysis
    indicators: IndicatorAnalysis
    regimes: RegimeAnalysis


class _Precomputed:
    """Stands in for one of RegimeEngine's internal engines and hands back a result already
    computed on the SAME bars with the SAME configuration, instead of computing it again
    (the Price Action Engine is the dominant cost). Any other input is computed normally, so
    the regime output is identical by construction (and tested to be)."""

    def __init__(self, engine: Any, bars: pd.DataFrame, result: Any) -> None:
        self._engine = engine
        self.config = engine.config  # RegimeEngine reads it for its fingerprint
        self._bars = bars
        self._result = result
        self.reused = 0

    def analyze(
        self, bars: pd.DataFrame, *, instrument_id: int | None = None, timeframe: str = "1d"
    ) -> Any:
        if (
            instrument_id is None
            and timeframe == "1d"
            and self._result.config_fingerprint == self.config.fingerprint()
            and bars.equals(self._bars)
        ):
            self.reused += 1
            return self._result
        return self._engine.analyze(bars, instrument_id=instrument_id, timeframe=timeframe)


def compute_engines(bars: pd.DataFrame) -> MarketEngines:
    ohlcv = bars[analysis.OHLCV]
    price_action = PriceActionEngine().analyze(ohlcv)
    indicators = IndicatorEngine().analyze(ohlcv)
    regime_engine = RegimeEngine()
    # Reuse the two results above inside the regime engine (no second Price Action run).
    regime_engine.price_action = cast(
        PriceActionEngine, _Precomputed(regime_engine.price_action, ohlcv, price_action)
    )
    regime_engine.indicators = cast(
        IndicatorEngine, _Precomputed(regime_engine.indicators, ohlcv, indicators)
    )
    return MarketEngines(
        bars=bars,
        candles=CandlestickEngine().analyze(ohlcv),
        price_action=price_action,
        indicators=indicators,
        regimes=regime_engine.analyze(ohlcv),
    )


def as_bundle(e: MarketEngines) -> analysis.EngineBundle:
    # The view functions only read .candles/.price_action/.indicators/.regimes.
    return cast(analysis.EngineBundle, e)


T = TypeVar("T")


class AnalysisCache:
    """Bounded LRU keyed by dataset identity. Thread-safe; one computation per key at a time
    (concurrent requests for the same key wait for it); failures are never cached."""

    def __init__(self, maxsize: int = 2) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self.hits = 0
        self.misses = 0
        self.computations = 0

    def get(self, key: str, compute: Callable[[], T]) -> T:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return cast(T, self._data[key])
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                if key in self._data:  # computed by a concurrent request meanwhile
                    self._data.move_to_end(key)
                    self.hits += 1
                    return cast(T, self._data[key])
                self.misses += 1
            try:
                value = compute()
                with self._lock:
                    self.computations += 1
                    self._data[key] = value
                    self._data.move_to_end(key)
                    while len(self._data) > self.maxsize:
                        self._data.popitem(last=False)
                return value
            finally:
                with self._lock:
                    self._key_locks.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


CACHE = AnalysisCache()


# ── data source ──


@dataclass(frozen=True)
class Loaded:
    ident: market.DatasetIdentity
    last_ingestion: dict[str, Any] | None
    engines: MarketEngines


class MarketSource(Protocol):
    def load(self, cache: AnalysisCache) -> Loaded: ...


class DbMarketSource:
    """Stored closed bars, read in one READ ONLY transaction (scoped to that transaction only:
    the pooled connection is returned unchanged)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def load(self, cache: AnalysisCache) -> Loaded:
        with Session(self.engine) as s:
            s.execute(text("SET TRANSACTION READ ONLY"))
            try:
                ident = market.dataset_identity(s, SYMBOL)
                last_run = market.latest_ingestion(s, ident.instrument_id)
                engines = cache.get(ident.key, lambda: compute_engines(market.load_bars(s, ident)))
            finally:
                s.rollback()
        return Loaded(ident, last_run, engines)


def get_market_source(engine: Annotated[Engine, Depends(get_engine)]) -> MarketSource:
    return DbMarketSource(engine)


def get_clock() -> datetime:
    return datetime.now(UTC)


def load(source: MarketSource) -> Loaded:
    try:
        return source.load(CACHE)
    except LookupError as exc:  # NoDataError and the loader's missing-instrument error
        raise HTTPException(status_code=404, detail="no stored market data") from exc
    except SQLAlchemyError as exc:
        # Only the exception class is logged: driver messages may contain host/user details.
        log.error("market.database_unavailable", error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable") from exc


def only_params(request: Request, allowed: frozenset[str]) -> None:
    extra = sorted(set(request.query_params) - allowed)
    if extra:
        raise HTTPException(status_code=422, detail=f"unsupported query parameter(s): {extra}")


# ── schemas ──

Scalar = float | int | str | bool | None


class Meta(BaseModel):
    source: Literal["LOCAL"]
    label: str
    symbol: str
    provider: str
    timeframe: str
    prices: str
    dataset_key: str
    generated_at: datetime
    engine_versions: dict[str, str]


class LastIngestion(BaseModel):
    run_id: int
    status: str
    finished_at: datetime | None


class Freshness(BaseModel):
    as_of: datetime
    last_bar_session: date
    expected_session: date | None
    missing_sessions: int
    stale: bool
    market_status: str


class Dataset(BaseModel):
    first_session: date
    last_session: date
    bars: int
    last_ingestion: LastIngestion | None
    freshness: Freshness


class LatestBar(BaseModel):
    session: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    previous_close: float | None
    change: float | None
    change_pct: float | None


class RegimeDimension(BaseModel):
    available: bool
    label: str | None
    age_sessions: int | None
    changed_on_session: bool
    previous_label: str | None


class Regimes(BaseModel):
    dimensions: dict[str, RegimeDimension]
    measures: dict[str, float | None]


class IndicatorRow(BaseModel):
    group: str
    indicator: str
    value: float | None
    status: Literal["ok", "warm-up", "undefined"]
    definition: str
    warm_up_bars: int


class Candles(BaseModel):
    geometry: dict[str, Scalar]
    patterns: list[dict[str, Scalar]]


class Swing(BaseModel):
    kind: str
    pivot_session: date
    price: float | None
    label: str | None
    confirmed_session: date


class StructureEvent(BaseModel):
    event_type: str
    direction: str | None
    session: date
    available_session: date
    reference_level: float | None
    close: float | None
    confirmation_type: str | None


class Structure(BaseModel):
    available: bool
    state: dict[str, Scalar]
    recent_swings: list[Swing]
    recent_events: list[StructureEvent]
    event_counts: dict[str, int]
    recent_sessions: int


class Summary(BaseModel):
    meta: Meta
    dataset: Dataset
    latest_bar: LatestBar
    regimes: Regimes
    indicators: list[IndicatorRow]
    candles: Candles
    structure: Structure


class Series(BaseModel):
    meta: Meta
    range: RangeKey
    sessions_requested: int
    sessions_returned: int
    sessions: list[date]
    open: list[float | None]
    high: list[float | None]
    low: list[float | None]
    close: list[float | None]
    volume: list[float | None]
    overlays: dict[str, list[float | None]]
    oscillators: dict[str, list[float | None]]


# ── builders (pure) ──


def build_meta(ld: Loaded, now: datetime) -> Meta:
    e = ld.engines
    return Meta(
        source="LOCAL",
        label=LABEL,
        symbol=ld.ident.symbol,
        provider=ld.ident.provider,
        timeframe=ld.ident.timeframe,
        prices=PRICES,
        dataset_key=ld.ident.key,
        generated_at=now,
        engine_versions={
            "candles": e.candles.engine_version,
            "price_action": e.price_action.engine_version,
            "indicators": e.indicators.engine_version,
            "regimes": e.regimes.engine_version,
        },
    )


def _label(v: Any) -> str | None:
    v = analysis._clean(v)
    return None if v is None or v == INSUFFICIENT else str(v)


def build_summary(ld: Loaded, now: datetime) -> Summary:
    e, bars = ld.engines, ld.engines.bars
    b = as_bundle(e)
    last_ts = bars.index[-1]
    fr = market.freshness(last_ts, now)
    ov = market.market_overview(bars)
    run = ld.last_ingestion

    rv = analysis.regime_view(b, last_ts)
    dims = {
        name: RegimeDimension(
            available=_label(d["label"]) is not None,
            label=_label(d["label"]),
            age_sessions=d["age_sessions"] if _label(d["label"]) is not None else None,
            changed_on_session=bool(d["changed_on_session"]),
            previous_label=_label(d["previous_label"]),
        )
        for name, d in rv["dimensions"].items()
    }

    pa = analysis.price_action_view(b, last_ts)
    state = {k: jsonable(v) for k, v in pa["state"].items()}
    structure_ok = state.get("structure") not in (None, INSUFFICIENT)
    if not structure_ok:
        state["structure"] = None
    swings = [
        Swing(
            kind=str(r["kind"]),
            pivot_session=session_of(r["pivot_ts"]),
            price=num(r["price"]),
            label=_label(r["label"]),
            confirmed_session=session_of(r["confirmed_at"]),
        )
        for _, r in pa["recent_swings"].iterrows()
    ]
    events = [
        StructureEvent(
            event_type=str(r["event_type"]),
            direction=_label(r["direction"]),
            session=session_of(r["ts"]),
            available_session=session_of(r["available_at"]),
            reference_level=num(r["reference_level"]),
            close=num(r["close"]),
            confirmation_type=_label(r["confirmation_type"]),
        )
        for _, r in pa["recent_events"].iterrows()
    ]

    cv = analysis.candle_view(b, last_ts)
    return Summary(
        meta=build_meta(ld, now),
        dataset=Dataset(
            first_session=session_of(ld.ident.first_ts),
            last_session=session_of(ld.ident.last_ts),
            bars=ld.ident.bars,
            last_ingestion=None
            if run is None
            else LastIngestion(
                run_id=run["run_id"], status=run["status"], finished_at=run["finished_at"]
            ),
            freshness=Freshness(
                as_of=fr.now.to_pydatetime(),
                last_bar_session=fr.last_bar_session,
                expected_session=fr.expected_session,
                missing_sessions=fr.missing_sessions,
                stale=fr.stale,
                market_status=fr.market_status,
            ),
        ),
        latest_bar=LatestBar(
            session=ov["session"],
            **{
                k: num(ov[k])
                for k in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "previous_close",
                    "change",
                    "change_pct",
                )
            },
        ),
        regimes=Regimes(dimensions=dims, measures={k: num(v) for k, v in rv["measures"].items()}),
        indicators=[
            IndicatorRow(
                group=str(r["group"]),
                indicator=str(r["indicator"]),
                value=num(r["value"]),
                status=r["status"],
                definition=str(r["definition"]),
                warm_up_bars=int(r["warm_up_bars"]),
            )
            for _, r in analysis.indicator_view(b, last_ts).iterrows()
        ],
        candles=Candles(
            geometry={k: jsonable(v) for k, v in cv["geometry"].items()},
            patterns=[{k: jsonable(v) for k, v in p.items()} for p in cv["patterns"]],
        ),
        structure=Structure(
            available=structure_ok,
            state=state,
            recent_swings=swings,
            recent_events=events,
            event_counts=pa["event_counts"],
            recent_sessions=pa["recent_sessions"],
        ),
    )


def build_series(ld: Loaded, now: datetime, rng: RangeKey) -> Series:
    n = RANGES[rng]
    bars = ld.engines.bars.iloc[-n:]
    ind = ld.engines.indicators.values.loc[bars.index]  # computed on the full history: causal
    return Series(
        meta=build_meta(ld, now),
        range=rng,
        sessions_requested=n,
        sessions_returned=len(bars),
        sessions=[session_of(t) for t in bars.index],
        open=series(bars["open"]),
        high=series(bars["high"]),
        low=series(bars["low"]),
        close=series(bars["close"]),
        volume=series(bars["volume"]),
        overlays={c: series(ind[c]) for c in OVERLAYS},
        oscillators={c: series(ind[c]) for c in OSCILLATORS},
    )


# ── routes ──

Source = Annotated[MarketSource, Depends(get_market_source)]
Clock = Annotated[datetime, Depends(get_clock)]


@router.get("/summary", response_model=Summary)
def summary(request: Request, source: Source, now: Clock) -> Summary:
    only_params(request, frozenset())
    return build_summary(load(source), now)


@router.get("/series", response_model=Series)
def series_endpoint(
    request: Request,
    source: Source,
    now: Clock,
    range_: Annotated[RangeKey, Query(alias="range")] = "1y",
) -> Series:
    only_params(request, frozenset({"range"}))
    return build_series(load(source), now, range_)
