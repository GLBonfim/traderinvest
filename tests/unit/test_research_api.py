"""Read-only research API (ADR-0029) on synthetic bars injected through dependencies.

Contracts, equivalence with the existing backtest services, slices, nulls, 404/422/503/405,
causality, cache (cold/warm, concurrency, failures, invalidation), the read-only transaction
and the import boundary."""

import ast
import json
import math
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.api import research as rs
from app.api.main import app
from app.backtest.config import BacktestConfig
from app.backtest.engine import run_suite
from app.backtest.metrics import drawdown
from app.dashboard.services import research as research_service
from app.dashboard.services.market import DatasetIdentity, NoDataError
from app.database.session import get_engine
from app.strategies.config import StrategyConfig
from app.strategies.engine import StrategyEngine
from tests.unit.strategies.helpers import session_walk

STRATEGIES = "/api/v1/research/strategies"
BACKTESTS = "/api/v1/research/backtests"
CURVES = "/api/v1/research/curves"
ORDER = [
    "buy_and_hold",
    "sma_trend",
    "sma_crossover",
    "rsi_momentum",
    "price_action_trend",
    "regime_trend",
    "benchmark_buy_and_hold_price",
    "benchmark_buy_and_hold_total",
]


def make_bars(n: int = 700, start: date = date(2008, 1, 2), seed: int = 21) -> pd.DataFrame:
    b = session_walk(n, seed=seed, start=start)
    return b.assign(adj_close=b["close"] * 0.98)


BARS = make_bars()  # 2008-01-02 .. ~2010-10: both the early and the late slice have sessions


def ident_of(bars: pd.DataFrame, ingested: str = "2020-01-01") -> DatasetIdentity:
    return DatasetIdentity(
        "SPY",
        1,
        "yfinance",
        "1d",
        len(bars),
        bars.index[0],
        bars.index[-1],
        pd.Timestamp(ingested, tz="UTC"),
    )


class FakeSource:
    """Same contract as the database source; counts bar loads (= suite computations)."""

    def __init__(self, bars: pd.DataFrame, ingested: str = "2020-01-01") -> None:
        self.bars = bars
        self.ingested = ingested
        self.loads = 0

    def _bars(self) -> pd.DataFrame:
        self.loads += 1
        return self.bars

    def load(self, slice_name: str) -> rs.SuiteLoaded:
        return rs.load_suite(ident_of(self.bars, self.ingested), self._bars, slice_name)


class RaisingSource:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def load(self, slice_name: str) -> rs.SuiteLoaded:
        raise self.exc


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    rs.SUITES.clear()
    rs.RESPONSES.clear()
    yield
    app.dependency_overrides.clear()
    rs.SUITES.clear()
    rs.RESPONSES.clear()


def use(source: Any) -> TestClient:
    app.dependency_overrides[rs.get_research_source] = lambda: source
    return TestClient(app)


def reference(slice_name: str, bars: pd.DataFrame = BARS) -> dict[str, Any]:
    s = rs.SLICES[slice_name]
    return run_suite(bars, config=BacktestConfig(start=s.start, end=s.end, period_label=s.name))


def clean(v: Any) -> float | None:
    f = float(v)
    return f if math.isfinite(f) else None


def test_slices_are_the_predeclared_validation_slices() -> None:
    from app.validation.config import DEFAULT_SLICES

    assert [(s.name, s.start, s.end) for s in rs.SLICES.values()] == [
        (s.name, s.start, s.end) for s in DEFAULT_SLICES
    ]


# ── /strategies ──


def test_strategies_contract() -> None:
    r = TestClient(app).get(STRATEGIES)
    assert r.status_code == 200
    b = r.json()
    se = StrategyEngine()
    assert b["meta"]["source"] == "LOCAL"
    assert [s["strategy_id"] for s in b["strategies"]] == ORDER[:6]
    assert b["strategy_engine_fingerprint"] == se.fingerprint()
    assert set(b["parameters"]) == set(vars(StrategyConfig()))
    assert b["parameters"]["sma_trend_period"] == 200
    for s, eng in zip(b["strategies"], se.strategies, strict=True):
        assert (s["version"], s["description"], s["features"]) == (
            eng.version,
            eng.description,
            list(eng.features),
        )
    kinds = {x["series_id"]: x["kind"] for x in b["benchmarks"]}
    assert kinds == {
        "benchmark_buy_and_hold_price": "benchmark_price",
        "benchmark_buy_and_hold_total": "benchmark_total",
    }


# ── /backtests ──


@pytest.mark.parametrize("slice_name", ["full", "early", "late"])
def test_backtests_equal_the_existing_backtester(slice_name: str) -> None:
    r = use(FakeSource(BARS)).get(BACKTESTS, params={"slice": slice_name})
    assert r.status_code == 200
    assert "NaN" not in r.text and "Infinity" not in r.text
    b = r.json()
    ref = reference(slice_name)
    assert [x["series_id"] for x in b["results"]] == ORDER  # fixed order, never ranked
    for x in b["results"]:
        expected = {k: clean(v) for k, v in ref[x["series_id"]].metrics.items()}
        assert x["metrics"] == pytest.approx(expected, nan_ok=False)
        assert x["config_fingerprint"] == ref[x["series_id"]].config_fingerprint
    s = rs.SLICES[slice_name]
    assert b["slice"] == {
        "name": slice_name,
        "start": s.start and s.start.isoformat(),
        "end": s.end and s.end.isoformat(),
        "out_of_sample": False,
        "note": rs.SLICE_NOTE,
    }
    eq = ref["sma_trend"].equity.index.tz_convert("America/New_York")
    assert b["window"] == {
        "first_session": eq[0].date().isoformat(),
        "last_session": eq[-1].date().isoformat(),
        "sessions": len(eq),
    }
    if s.start:
        assert b["window"]["first_session"] >= s.start.isoformat()
    if s.end:
        assert b["window"]["last_session"] <= s.end.isoformat()


def test_backtests_match_the_dashboard_research_service() -> None:
    b = use(FakeSource(BARS)).get(BACKTESTS).json()
    table = research_service.backtest_suite(BARS)["table"]
    for x in b["results"]:
        for col in research_service.BACKTEST_COLUMNS:
            assert x["metrics"][col] == pytest.approx(clean(table.loc[x["series_id"], col])), col


def test_backtests_state_assumptions_and_benchmarks() -> None:
    b = use(FakeSource(BARS)).get(BACKTESTS, params={"slice": "late"}).json()
    cfg = rs.slice_config("late")
    a = b["assumptions"]
    assert a["execution"] == "next_session_open" and a["config_fingerprint"] == cfg.fingerprint()
    assert {k: a["costs"][k] for k in ("commission_per_trade", "spread_bps", "slippage_bps")} == {
        "commission_per_trade": cfg.commission_per_trade,
        "spread_bps": cfg.spread_bps,
        "slippage_bps": cfg.slippage_bps,
    }
    assert "not broker quotes" in a["costs"]["note"]
    assert a["strategy_engine_fingerprint"] == StrategyEngine().fingerprint()
    kinds = {x["series_id"]: x["kind"] for x in b["results"]}
    assert kinds["benchmark_buy_and_hold_price"] == "benchmark_price"
    assert kinds["benchmark_buy_and_hold_total"] == "benchmark_total"
    assert b["data"]["total_return_benchmark_available"] is True
    assert b["meta"]["source"] == "LOCAL" and b["meta"]["dataset_key"] == ident_of(BARS).key


def test_total_return_benchmark_absent_without_adjusted_prices() -> None:
    bars = BARS.assign(adj_close=float("nan"))
    b = use(FakeSource(bars)).get(BACKTESTS).json()
    assert b["data"]["total_return_benchmark_available"] is False
    assert "benchmark_buy_and_hold_total" not in [x["series_id"] for x in b["results"]]


def test_non_finite_metrics_become_null() -> None:
    ld = FakeSource(BARS).load("full")
    ld.results["sma_trend"].metrics.update(sharpe=float("nan"), sortino=float("inf"))
    out = rs.build_backtests(ld)
    m = next(x for x in out.results if x.series_id == "sma_trend").metrics
    assert m["sharpe"] is None and m["sortino"] is None
    assert "NaN" not in out.model_dump_json() and "Infinity" not in out.model_dump_json()


# ── /curves ──


@pytest.mark.parametrize("kind", ["net", "gross"])
@pytest.mark.parametrize("slice_name", ["full", "early", "late"])
def test_curves_equal_the_reference(slice_name: str, kind: str) -> None:
    r = use(FakeSource(BARS)).get(CURVES, params={"slice": slice_name, "kind": kind})
    assert r.status_code == 200
    b = r.json()
    ref = reference(slice_name)
    assert list(b["series"]) == ORDER
    cum = research_service.equity_curves(ref, gross=kind == "gross")
    n = len(cum)
    assert len(b["sessions"]) == n and b["sessions"] == sorted(b["sessions"])
    for sid, c in b["series"].items():
        assert c["cumulative_return"] == pytest.approx(cum[sid].tolist())
        eq = ref[sid].equity
        if kind == "net":
            expected_dd = research_service.drawdown_curves(ref)[sid]
        else:
            expected_dd = drawdown(eq["gross_equity"], 100_000.0)["drawdown"]
        assert c["drawdown"] == pytest.approx(expected_dd.tolist())
        assert all(v is None or v <= 1e-12 for v in c["drawdown"])


def test_gross_drawdown_uses_the_engine_definition() -> None:
    eq = reference("full")["sma_trend"].equity
    # the engine's own net drawdown column is reproduced by the same function
    assert drawdown(eq["equity"], 100_000.0)["drawdown"].equals(eq["drawdown"])


def test_curves_are_causal_appending_bars_changes_nothing_before() -> None:
    short = use(FakeSource(BARS.iloc[:500])).get(CURVES).json()
    rs.SUITES.clear()
    rs.RESPONSES.clear()
    full = use(FakeSource(BARS)).get(CURVES).json()
    k = len(short["sessions"])
    assert full["sessions"][:k] == short["sessions"]
    for sid in ORDER:
        for col in ("cumulative_return", "drawdown"):
            assert full["series"][sid][col][:k] == pytest.approx(short["series"][sid][col])


def test_slice_features_use_bars_up_to_the_slice_end_only() -> None:
    """Mutating every bar after the early slice end changes nothing in the early results."""
    later = pd.DatetimeIndex(BARS.index).tz_convert("America/New_York").date > date(2009, 12, 31)
    mutated = BARS.copy()
    mutated.loc[later, ["open", "high", "low", "close"]] *= 1.5
    a = use(FakeSource(BARS)).get(BACKTESTS, params={"slice": "early"}).json()
    rs.SUITES.clear()
    rs.RESPONSES.clear()
    b = (
        use(FakeSource(mutated, ingested="2021-01-01"))
        .get(BACKTESTS, params={"slice": "early"})
        .json()
    )
    assert [x["metrics"] for x in a["results"]] == [x["metrics"] for x in b["results"]]


# ── errors and parameters ──


def test_slice_without_sessions_is_404() -> None:
    bars_2018 = make_bars(300, start=date(2018, 1, 2))
    r = use(FakeSource(bars_2018)).get(BACKTESTS, params={"slice": "early"})
    assert (r.status_code, r.json()["detail"]) == (404, "no stored data for this slice")


@pytest.mark.parametrize("exc", [NoDataError("none"), LookupError("instrument 'SPY' not found")])
def test_no_stored_data_is_404(exc: Exception) -> None:
    for path in (BACKTESTS, CURVES):
        assert use(RaisingSource(exc)).get(path).status_code == 404


def test_unreachable_database_is_503_without_details() -> None:
    dead = create_engine(
        "postgresql+psycopg://nobody:nopass@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    app.dependency_overrides[get_engine] = lambda: dead  # the real DbResearchSource path
    client = TestClient(app)
    for path in (BACKTESTS, CURVES):
        r = client.get(path)
        assert (r.status_code, r.json()) == (503, {"detail": "database unavailable"})
        assert "nopass" not in r.text


@pytest.mark.parametrize(
    ("path", "query"),
    [
        (BACKTESTS, "slice=test"),
        (BACKTESTS, "slice="),
        (BACKTESTS, "slice=full&strategy=sma_trend"),
        (BACKTESTS, "slice=full&start=2000-01-01"),
        (CURVES, "kind=log"),
        (CURVES, "slice=oos"),
        (CURVES, "slice=full&kind=net&symbol=QQQ"),
        (STRATEGIES, "symbol=QQQ"),
    ],
)
def test_invalid_or_unknown_parameters_are_422(path: str, query: str) -> None:
    assert use(FakeSource(BARS)).get(f"{path}?{query}").status_code == 422


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", [STRATEGIES, BACKTESTS, CURVES])
def test_write_methods_are_rejected(method: str, path: str) -> None:
    src = FakeSource(BARS)
    assert use(src).request(method, path, content=b"{}").status_code == 405
    assert src.loads == 0


# ── cache ──


def test_one_suite_computation_per_dataset_and_slice() -> None:
    src = FakeSource(BARS)
    client = use(src)
    for q in ("", "?kind=gross", "?slice=full"):
        assert client.get(CURVES + q).status_code == 200
    assert client.get(BACKTESTS).status_code == 200
    assert src.loads == 1  # backtests + net + gross curves of the same slice share one suite
    assert client.get(BACKTESTS + "?slice=late").status_code == 200
    assert src.loads == 2  # another slice -> its own suite
    src.ingested = "2020-01-02"  # new ingestion -> new dataset key -> recomputed
    assert client.get(BACKTESTS).status_code == 200
    assert src.loads == 3


def test_warm_responses_are_identical_and_fast() -> None:
    client = use(FakeSource(BARS))
    cold = client.get(CURVES)
    t = time.perf_counter()
    warm = client.get(CURVES)
    elapsed = time.perf_counter() - t
    assert warm.content == cold.content
    assert elapsed < 0.5  # generous bound for CI noise; measured values are reported separately


def test_failed_computation_is_not_cached() -> None:
    calls = []

    def flaky() -> pd.DataFrame:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient failure")
        return BARS

    with pytest.raises(RuntimeError):
        rs.load_suite(ident_of(BARS), flaky, "full")
    ld = rs.load_suite(ident_of(BARS), flaky, "full")
    assert len(calls) == 2 and ld.results


def test_concurrent_requests_compute_a_suite_once() -> None:
    calls = []

    def slow() -> pd.DataFrame:
        calls.append(1)
        time.sleep(0.2)
        return BARS

    out: list[rs.SuiteLoaded] = []
    threads = [
        threading.Thread(target=lambda: out.append(rs.load_suite(ident_of(BARS), slow, "late")))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and len(out) == 6
    assert all(o.results is out[0].results for o in out)


# ── read-only database access ──


class RecordingSession:
    log: ClassVar[list[str]] = []

    def __init__(self, engine: Any) -> None:
        pass

    def __enter__(self) -> "RecordingSession":
        return self

    def __exit__(self, *exc: object) -> None:
        RecordingSession.log.append("close")

    def execute(self, stmt: Any) -> None:
        RecordingSession.log.append(str(stmt))

    def rollback(self) -> None:
        RecordingSession.log.append("rollback")

    def commit(self) -> None:  # pragma: no cover - must never be called
        RecordingSession.log.append("COMMIT")


def test_db_source_reads_in_one_read_only_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingSession.log = []

    def identity(s: Any, symbol: str) -> DatasetIdentity:
        RecordingSession.log.append(f"identity:{symbol}")
        return ident_of(BARS)

    def bars(s: Any, ident: DatasetIdentity) -> pd.DataFrame:
        RecordingSession.log.append("load_bars")
        return BARS

    monkeypatch.setattr(rs, "Session", RecordingSession)
    monkeypatch.setattr(rs.market, "dataset_identity", identity)
    monkeypatch.setattr(rs.market, "load_bars", bars)
    ld = rs.DbResearchSource(object()).load("late")  # type: ignore[arg-type]
    assert RecordingSession.log == [
        "SET TRANSACTION READ ONLY",
        "identity:SPY",
        "load_bars",
        "rollback",
        "close",
    ]
    RecordingSession.log = []
    rs.DbResearchSource(object()).load("late")  # type: ignore[arg-type]
    assert "load_bars" not in RecordingSession.log  # cached suite: bars are not read again
    assert ld.results


# ── import boundary ──

FORBIDDEN_PREFIXES = (
    "app.broker",
    "app.paper",
    "app.operations",
    "app.alerts",
    "app.data.ingestion",
    "app.data.providers",
    "app.dashboard.services.research",
    "app.dashboard.services.broker",
    "app.dashboard.services.paper",
    "app.dashboard.ui",
    "app.ml",
    "app.validation.engine",
    "sklearn",
    "yfinance",
    "streamlit",
    "requests",
    "httpx",
)


def test_research_module_imports_nothing_operational() -> None:
    tree = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    assert not [n for n in names if n.startswith(FORBIDDEN_PREFIXES)]


def test_importing_the_api_loads_no_operational_or_ml_module() -> None:
    code = (
        "import json, sys; import app.api.main; "
        f"p = {FORBIDDEN_PREFIXES!r}; "
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith(p))))"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def test_computed_at_is_when_the_suite_was_computed() -> None:
    before = datetime.now(UTC)
    b = use(FakeSource(BARS)).get(BACKTESTS).json()
    assert before <= datetime.fromisoformat(b["meta"]["computed_at"]) <= datetime.now(UTC)
