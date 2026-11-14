"""Read-only research endpoints for the web UI (ADR-0029).

GET /api/v1/research/strategies                       fixed baseline rules and their parameters
GET /api/v1/research/backtests?slice=full|early|late  Phase 8 metrics of every strategy and both
                                                      buy & hold benchmarks
GET /api/v1/research/curves?slice=…&kind=net|gross    cumulative return and drawdown per series

SPY only. Results come from the existing backtester (`app.backtest.engine.run_suite`, next-session
open execution, the default generic cost assumptions) on the stored closed daily bars; nothing is
re-implemented, tuned or ranked. Slices are the pre-declared Phase 9 date ranges: descriptive
sub-periods, NOT out-of-sample tests (the baselines have fixed textbook parameters and are never
fitted). One READ ONLY database transaction per request; nothing is written anywhere. No
statistical validation, machine learning or walk-forward here (the dashboard research service is
deliberately not imported: it would load the ML stack).
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, Protocol

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.market import (
    LABEL,
    SOURCE,
    SYMBOL,
    AnalysisCache,
    jsonable,
    num,
    only_params,
    session_of,
)
from app.backtest.config import ENGINE_VERSION as BACKTEST_ENGINE_VERSION
from app.backtest.config import BacktestConfig
from app.backtest.engine import BENCHMARK_PRICE, BENCHMARK_TOTAL, run_suite
from app.backtest.metrics import drawdown
from app.backtest.models import BacktestResult
from app.core.logging import get_logger
from app.dashboard.services import market
from app.database.session import get_engine
from app.strategies.config import ENGINE_VERSION as STRATEGY_ENGINE_VERSION
from app.strategies.engine import StrategyEngine

router = APIRouter(prefix="/api/v1/research", tags=["research"])
log = get_logger(__name__)

SliceKey = Literal["full", "early", "late"]
CurveKind = Literal["net", "gross"]


@dataclass(frozen=True)
class SliceSpec:
    name: str
    start: date | None = None
    end: date | None = None


# The pre-declared Phase 9 slices (app.validation.config.DEFAULT_SLICES), restated here because
# importing that package would load the statistical validation engine into the API process;
# a unit test asserts both definitions are identical.
SLICES: dict[str, SliceSpec] = {
    "full": SliceSpec("full"),
    "early": SliceSpec("early", end=date(2009, 12, 31)),
    "late": SliceSpec("late", start=date(2010, 1, 1)),
}
SLICE_NOTE = (
    "Descriptive sub-period declared before any result was seen. The strategies have fixed "
    "textbook parameters and are never fitted, so a slice is not an out-of-sample test."
)
COST_NOTE = (
    "Generic research cost assumptions (not broker quotes), never calibrated on results: "
    "commission per order plus half of the quoted spread plus slippage, on each order."
)
BENCHMARKS = {
    BENCHMARK_PRICE: (
        "benchmark_price",
        "SPY buy & hold valued with raw prices (price return, no dividends): the like-for-like "
        "reference for the timing strategies, which also earn price return only.",
    ),
    BENCHMARK_TOTAL: (
        "benchmark_total",
        "SPY buy & hold valued with the provider's adjusted close (dividends reinvested). Not "
        "like-for-like with the price-only strategies; shown to make the dividend gap visible.",
    ),
}
NOTES = [
    "Historical simulations on one instrument and one path: not evidence of future profitability.",
    "Series are listed in a fixed order (strategies, then benchmarks); nothing is ranked.",
    "Execution at the next session's raw open; positions are all-in/all-out, long or flat.",
]


# ── engine identity and cache ──


@dataclass(frozen=True)
class SuiteLoaded:
    ident: market.DatasetIdentity
    slice: SliceSpec
    config: BacktestConfig
    strategy_fingerprint: str
    key: str
    results: dict[str, BacktestResult]
    computed_at: datetime


_strategy_engine: StrategyEngine | None = None


def strategy_engine() -> StrategyEngine:
    """The default baseline strategy engine (fixed parameters); built once per process."""
    global _strategy_engine
    if _strategy_engine is None:
        _strategy_engine = StrategyEngine()
    return _strategy_engine


def slice_config(name: str) -> BacktestConfig:
    s = SLICES[name]
    return BacktestConfig(start=s.start, end=s.end, period_label=s.name)


def suite_key(ident: market.DatasetIdentity, config: BacktestConfig) -> str:
    """Dataset identity (changes with every ingestion) + strategy engine + backtest config
    (which includes the slice dates)."""
    return f"{ident.key}|{strategy_engine().fingerprint()}|{config.fingerprint()}"


SUITES = AnalysisCache(maxsize=6)  # suites: 3 slices x 2 dataset versions
RESPONSES = AnalysisCache(maxsize=24)  # serialized responses derived from cached suites


def window_sessions(bars: pd.DataFrame, s: SliceSpec) -> int:
    days = pd.DatetimeIndex(bars.index).tz_convert("America/New_York").date
    mask = pd.Series(True, index=bars.index)
    if s.start:
        mask &= days >= s.start
    if s.end:
        mask &= days <= s.end
    return int(mask.sum())


def compute_suite(bars: pd.DataFrame, s: SliceSpec, config: BacktestConfig) -> Any:
    if window_sessions(bars, s) < 2:
        raise market.NoDataError(f"not enough stored sessions in slice {s.name!r}")
    # a fresh engine per computation (same default configuration and fingerprint): concurrent
    # computations of different slices never share engine objects
    return run_suite(bars, config=config, strategy_engine=StrategyEngine())


def load_suite(
    ident: market.DatasetIdentity, bars: Callable[[], pd.DataFrame], name: str
) -> SuiteLoaded:
    cfg = slice_config(name)
    key = suite_key(ident, cfg)
    results, computed_at = SUITES.get(
        key, lambda: (compute_suite(bars(), SLICES[name], cfg), datetime.now(UTC))
    )
    return SuiteLoaded(
        ident, SLICES[name], cfg, strategy_engine().fingerprint(), key, results, computed_at
    )


class ResearchSource(Protocol):
    def load(self, slice_name: str) -> SuiteLoaded: ...


class DbResearchSource:
    """Stored closed bars, read in one READ ONLY transaction (rolled back; the pooled
    connection is returned unchanged). Bars are loaded only when the suite is not cached."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def load(self, slice_name: str) -> SuiteLoaded:
        with Session(self.engine) as s:
            s.execute(text("SET TRANSACTION READ ONLY"))
            try:
                ident = market.dataset_identity(s, SYMBOL)
                return load_suite(ident, lambda: market.load_bars(s, ident), slice_name)
            finally:
                s.rollback()


def get_research_source(engine: Annotated[Engine, Depends(get_engine)]) -> ResearchSource:
    return DbResearchSource(engine)


def load(source: ResearchSource, slice_name: str) -> SuiteLoaded:
    try:
        return source.load(slice_name)
    except LookupError as exc:  # NoDataError, missing instrument, empty slice
        raise HTTPException(status_code=404, detail="no stored data for this slice") from exc
    except SQLAlchemyError as exc:
        log.error("research.database_unavailable", error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable") from exc


# ── schemas ──


class Meta(BaseModel):
    source: Literal["LOCAL"]
    label: str
    symbol: str
    provider: str
    timeframe: str
    dataset_key: str
    computed_at: datetime
    engine_versions: dict[str, str]


class StrategyInfo(BaseModel):
    strategy_id: str
    version: str
    description: str
    features: list[str]


class BenchmarkInfo(BaseModel):
    series_id: str
    kind: str
    description: str


class Strategies(BaseModel):
    meta: Meta
    strategy_engine_fingerprint: str
    parameters: dict[str, Any]
    rules: list[str]
    strategies: list[StrategyInfo]
    benchmarks: list[BenchmarkInfo]


class Costs(BaseModel):
    commission_per_trade: float
    commission_bps: float
    minimum_commission: float
    spread_bps: float
    slippage_bps: float
    note: str


class SliceInfo(BaseModel):
    name: SliceKey
    start: date | None
    end: date | None
    out_of_sample: Literal[False]
    note: str


class Window(BaseModel):
    first_session: date
    last_session: date
    sessions: int


class Assumptions(BaseModel):
    execution: str
    initial_capital: float
    periods_per_year: int
    risk_free_rate: float
    costs: Costs
    config_fingerprint: str
    strategy_engine_fingerprint: str


class DataInfo(BaseModel):
    first_session: date
    last_session: date
    bars: int
    total_return_benchmark_available: bool


class SeriesResult(BaseModel):
    series_id: str
    kind: Literal["strategy", "benchmark_price", "benchmark_total"]
    description: str
    metrics: dict[str, float | None]
    open_position: dict[str, Any] | None
    pending_state: dict[str, Any] | None
    config_fingerprint: str


class Backtests(BaseModel):
    meta: Meta
    slice: SliceInfo
    window: Window
    assumptions: Assumptions
    data: DataInfo
    results: list[SeriesResult]
    notes: list[str]


class Curve(BaseModel):
    cumulative_return: list[float | None]
    drawdown: list[float | None]


class Curves(BaseModel):
    meta: Meta
    slice: SliceInfo
    kind: CurveKind
    window: Window
    config_fingerprint: str
    sessions: list[date]
    series: dict[str, Curve]
    notes: list[str]


# ── builders (pure) ──


def build_meta(ident: market.DatasetIdentity, computed_at: datetime) -> Meta:
    return Meta(
        source=SOURCE,
        label=LABEL,
        symbol=ident.symbol,
        provider=ident.provider,
        timeframe=ident.timeframe,
        dataset_key=ident.key,
        computed_at=computed_at,
        engine_versions={
            "backtest": BACKTEST_ENGINE_VERSION,
            "strategies": STRATEGY_ENGINE_VERSION,
        },
    )


def _slice(s: SliceSpec) -> SliceInfo:
    return SliceInfo(name=s.name, start=s.start, end=s.end, out_of_sample=False, note=SLICE_NOTE)


def _ordered(results: dict[str, BacktestResult]) -> list[str]:
    """Fixed, metric-independent order: strategies as the engine lists them, then benchmarks."""
    ids = [s.strategy_id for s in strategy_engine().strategies if s.strategy_id in results]
    return ids + [b for b in BENCHMARKS if b in results]


def _window(ld: SuiteLoaded) -> Window:
    eq = next(iter(ld.results.values())).equity
    return Window(
        first_session=session_of(eq.index[0]),
        last_session=session_of(eq.index[-1]),
        sessions=len(eq),
    )


def build_strategies(computed_at: datetime) -> Strategies:
    se = strategy_engine()
    descriptions = {s.strategy_id: s.description for s in se.strategies}
    return Strategies(
        meta=Meta(
            source=SOURCE,
            label=LABEL,
            symbol=SYMBOL,
            provider="yfinance",
            timeframe="1d",
            dataset_key="n/a (configuration only)",
            computed_at=computed_at,
            engine_versions={"strategies": STRATEGY_ENGINE_VERSION},
        ),
        strategy_engine_fingerprint=se.fingerprint(),
        parameters={k: jsonable(v) for k, v in asdict(se.config).items()},
        rules=[
            "States are LONG or FLAT only (no shorting, no leverage); INSUFFICIENT_DATA keeps "
            "the previous target.",
            "A state decided at the close of session T is executed at the open of the next "
            "XNYS session.",
            "Parameters are fixed textbook conventions; nothing is optimised.",
        ],
        strategies=[
            StrategyInfo(
                strategy_id=s.strategy_id,
                version=s.version,
                description=descriptions[s.strategy_id],
                features=list(s.features),
            )
            for s in se.strategies
        ],
        benchmarks=[
            BenchmarkInfo(series_id=k, kind=v[0], description=v[1]) for k, v in BENCHMARKS.items()
        ],
    )


def jsonable_dict(d: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if d is None else {k: jsonable(v) for k, v in d.items()}


def build_backtests(ld: SuiteLoaded) -> Backtests:
    cfg = ld.config
    descriptions = {s.strategy_id: s.description for s in strategy_engine().strategies}
    results = []
    for sid in _ordered(ld.results):
        r = ld.results[sid]
        kind = BENCHMARKS[sid][0] if sid in BENCHMARKS else "strategy"
        results.append(
            SeriesResult(
                series_id=sid,
                kind=kind,
                description=BENCHMARKS[sid][1] if sid in BENCHMARKS else descriptions[sid],
                metrics={k: num(v) for k, v in r.metrics.items()},
                open_position=jsonable_dict(r.open_position),
                pending_state=jsonable_dict(r.pending_state),
                config_fingerprint=r.config_fingerprint,
            )
        )
    return Backtests(
        meta=build_meta(ld.ident, ld.computed_at),
        slice=_slice(ld.slice),
        window=_window(ld),
        assumptions=Assumptions(
            execution=cfg.execution,
            initial_capital=cfg.initial_capital,
            periods_per_year=cfg.periods_per_year,
            risk_free_rate=cfg.risk_free_rate,
            costs=Costs(
                commission_per_trade=cfg.commission_per_trade,
                commission_bps=cfg.commission_bps,
                minimum_commission=cfg.minimum_commission,
                spread_bps=cfg.spread_bps,
                slippage_bps=cfg.slippage_bps,
                note=COST_NOTE,
            ),
            config_fingerprint=cfg.fingerprint(),
            strategy_engine_fingerprint=ld.strategy_fingerprint,
        ),
        data=DataInfo(
            first_session=session_of(ld.ident.first_ts),
            last_session=session_of(ld.ident.last_ts),
            bars=ld.ident.bars,
            total_return_benchmark_available=BENCHMARK_TOTAL in ld.results,
        ),
        results=results,
        notes=[SLICE_NOTE, COST_NOTE, *NOTES],
    )


def build_curves(ld: SuiteLoaded, kind: CurveKind) -> Curves:
    ids = _ordered(ld.results)
    index = ld.results[ids[0]].equity.index
    series = {}
    for sid in ids:
        eq = ld.results[sid].equity
        if not eq.index.equals(index):  # every series of a suite shares the window
            raise RuntimeError("series windows differ")
        if kind == "net":
            cum, dd = eq["cumulative_return"], eq["drawdown"]
        else:
            # same drawdown definition as the engine (peak includes initial capital)
            cum = eq["gross_cumulative_return"]
            dd = drawdown(eq["gross_equity"], ld.config.initial_capital)["drawdown"]
        series[sid] = Curve(
            cumulative_return=[num(v) for v in cum.to_numpy()],
            drawdown=[num(v) for v in dd.to_numpy()],
        )
    notes = [SLICE_NOTE, *NOTES]
    if kind == "gross":
        notes.insert(0, "Gross curves: the same fills with all costs set to zero.")
    return Curves(
        meta=build_meta(ld.ident, ld.computed_at),
        slice=_slice(ld.slice),
        kind=kind,
        window=_window(ld),
        config_fingerprint=ld.config.fingerprint(),
        sessions=[session_of(t) for t in index],
        series=series,
        notes=notes,
    )


def cached_json(key: str, build: Callable[[], BaseModel]) -> Response:
    """Responses derived from a cached suite are validated once and kept as JSON bytes."""
    body = RESPONSES.get(key, lambda: build().model_dump_json().encode())
    return Response(content=body, media_type="application/json")


# ── routes ──

Source = Annotated[ResearchSource, Depends(get_research_source)]


@router.get("/strategies", response_model=Strategies)
def strategies(request: Request) -> Response:
    only_params(request, frozenset())
    se_fp = strategy_engine().fingerprint()
    return cached_json(f"strategies|{se_fp}", lambda: build_strategies(datetime.now(UTC)))


@router.get("/backtests", response_model=Backtests)
def backtests(
    request: Request,
    source: Source,
    slice_: Annotated[SliceKey, Query(alias="slice")] = "full",
) -> Response:
    only_params(request, frozenset({"slice"}))
    ld = load(source, slice_)
    return cached_json(f"{ld.key}|backtests", lambda: build_backtests(ld))


@router.get("/curves", response_model=Curves)
def curves(
    request: Request,
    source: Source,
    slice_: Annotated[SliceKey, Query(alias="slice")] = "full",
    kind: Annotated[CurveKind, Query()] = "net",
) -> Response:
    only_params(request, frozenset({"slice", "kind"}))
    ld = load(source, slice_)
    return cached_json(f"{ld.key}|curves|{kind}", lambda: build_curves(ld, kind))
