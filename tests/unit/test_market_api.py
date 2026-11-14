"""Read-only market API (ADR-0028) on synthetic data injected through dependencies — no database.

Contracts, ranges, nulls, 404/422/503, write methods, point-in-time behaviour, cache and the
read-only transaction of the real data source, and the import boundary."""

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

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.api import market as m
from app.api.main import app
from app.dashboard.services.market import DatasetIdentity, NoDataError
from app.database.session import get_engine
from app.indicators.engine import IndicatorEngine
from tests.unit.strategies.helpers import session_walk

N = 460
NOW = datetime(2020, 1, 6, 23, 0, tzinfo=UTC)
SUMMARY = "/api/v1/market/summary"
SERIES = "/api/v1/market/series"


def make_bars(n: int = N, seed: int = 13) -> pd.DataFrame:
    b = session_walk(n, seed=seed, start=date(2018, 1, 2))
    return b.assign(adj_close=b["close"] * 0.99)


BARS = make_bars()


class FakeSource:
    """Stands in for the database: same Loaded contract, counts engine computations."""

    def __init__(self, bars: pd.DataFrame, ingested: str = "2020-01-01") -> None:
        self.bars = bars
        self.ingested = pd.Timestamp(ingested, tz="UTC")
        self.computed = 0

    def load(self, cache: m.AnalysisCache) -> m.Loaded:
        b = self.bars
        ident = DatasetIdentity(
            "SPY", 1, "yfinance", "1d", len(b), b.index[0], b.index[-1], self.ingested
        )

        def compute() -> m.MarketEngines:
            self.computed += 1
            return m.compute_engines(b)

        run = {"run_id": 7, "status": "succeeded", "finished_at": datetime(2020, 1, 1, tzinfo=UTC)}
        return m.Loaded(ident, run, cache.get(ident.key, compute))


class RaisingSource:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def load(self, cache: m.AnalysisCache) -> m.Loaded:
        raise self.exc


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    m.CACHE.clear()
    app.dependency_overrides[m.get_clock] = lambda: NOW
    yield
    app.dependency_overrides.clear()
    m.CACHE.clear()


def use(source: Any) -> TestClient:
    app.dependency_overrides[m.get_market_source] = lambda: source
    return TestClient(app)


@pytest.fixture(scope="module")
def summary_body() -> dict[str, Any]:
    m.CACHE.clear()
    app.dependency_overrides[m.get_clock] = lambda: NOW
    app.dependency_overrides[m.get_market_source] = lambda: FakeSource(BARS)
    try:
        r = TestClient(app).get(SUMMARY)
    finally:
        app.dependency_overrides.clear()
        m.CACHE.clear()
    assert r.status_code == 200
    assert "NaN" not in r.text and "Infinity" not in r.text
    return dict(r.json())


# ── contracts ──


def test_summary_contract(summary_body: dict[str, Any]) -> None:
    b = summary_body
    assert set(b) == {
        "meta",
        "dataset",
        "latest_bar",
        "regimes",
        "indicators",
        "candles",
        "structure",
    }
    meta = b["meta"]
    assert meta["source"] == "LOCAL" and meta["label"] == "NOT LIVE · END-OF-DAY RESEARCH DATA"
    assert (meta["symbol"], meta["provider"], meta["timeframe"]) == ("SPY", "yfinance", "1d")
    assert meta["prices"] == "raw, dividend-unadjusted"
    assert set(meta["engine_versions"]) == {"candles", "price_action", "indicators", "regimes"}
    last = BARS.index[-1].tz_convert("America/New_York").date().isoformat()
    first = BARS.index[0].tz_convert("America/New_York").date().isoformat()
    ds = b["dataset"]
    assert (ds["first_session"], ds["last_session"], ds["bars"]) == (first, last, N)
    assert ds["last_ingestion"]["run_id"] == 7
    assert b["latest_bar"]["session"] == last
    assert b["latest_bar"]["close"] == pytest.approx(BARS["close"].iloc[-1])
    assert b["latest_bar"]["previous_close"] == pytest.approx(BARS["close"].iloc[-2])


def test_freshness_is_evaluated_at_the_request_clock(summary_body: dict[str, Any]) -> None:
    fr = summary_body["dataset"]["freshness"]
    assert datetime.fromisoformat(fr["as_of"]) == NOW
    assert fr["last_bar_session"] == summary_body["dataset"]["last_session"]
    assert fr["stale"] == (fr["missing_sessions"] > 0)


def test_indicator_rows_keep_their_status(summary_body: dict[str, Any]) -> None:
    rows = summary_body["indicators"]
    assert {r["status"] for r in rows} <= {"ok", "warm-up", "undefined"}
    assert all((r["value"] is None) == (r["status"] != "ok") for r in rows)
    assert {"sma_200", "rsi_14", "macd", "atr_14", "obv"} <= {r["indicator"] for r in rows}


def test_regimes_available_on_long_history(summary_body: dict[str, Any]) -> None:
    dims = summary_body["regimes"]["dimensions"]
    assert set(dims) == {"trend", "volatility", "momentum", "participation", "composite"}
    for d in dims.values():
        assert d["available"] == (d["label"] is not None)
        assert d["label"] != "insufficient_data" and d["previous_label"] != "insufficient_data"


def test_short_history_reports_unavailable_regimes_not_labels() -> None:
    r = use(FakeSource(make_bars(120, seed=3))).get(SUMMARY)
    assert r.status_code == 200
    body = r.json()
    dims = body["regimes"]["dimensions"]
    assert not dims["volatility"]["available"]  # needs >= 252 references (ADR-0015)
    assert not dims["composite"]["available"]
    for d in dims.values():
        if not d["available"]:
            assert d["label"] is None and d["age_sessions"] is None
    st = body["structure"]
    assert st["available"] == (st["state"]["structure"] is not None)
    assert "insufficient_data" not in r.text


def test_structure_is_point_in_time(summary_body: dict[str, Any]) -> None:
    last = summary_body["dataset"]["last_session"]
    st = summary_body["structure"]
    assert st["recent_swings"]
    for s in st["recent_swings"]:
        assert s["pivot_session"] <= s["confirmed_session"] <= last  # confirmation latency kept
    for e in st["recent_events"]:
        assert e["session"] <= e["available_session"] <= last
    assert set(st["event_counts"]) == {"breakout", "breakdown", "retest", "rejection", "sweep"}


def test_candle_observations_are_for_the_latest_session(summary_body: dict[str, Any]) -> None:
    c = summary_body["candles"]
    assert "body_ratio" in c["geometry"] and isinstance(c["patterns"], list)


# ── series ──


@pytest.mark.parametrize(("rng", "n"), list(m.RANGES.items()))
def test_series_ranges(rng: str, n: int) -> None:
    r = use(FakeSource(BARS)).get(SERIES, params={"range": rng})
    assert r.status_code == 200
    b = r.json()
    expected = min(n, N)
    assert (b["range"], b["sessions_requested"], b["sessions_returned"]) == (rng, n, expected)
    arrays = [b[k] for k in ("sessions", "open", "high", "low", "close", "volume")]
    arrays += list(b["overlays"].values()) + list(b["oscillators"].values())
    assert {len(a) for a in arrays} == {expected}
    assert b["sessions"] == sorted(b["sessions"]) and len(set(b["sessions"])) == expected
    assert set(b["overlays"]) == set(m.OVERLAYS) and set(b["oscillators"]) == set(m.OSCILLATORS)


def test_series_default_range_is_one_year() -> None:
    b = use(FakeSource(BARS)).get(SERIES).json()
    assert (b["range"], b["sessions_returned"]) == ("1y", 252)


def test_series_values_come_from_the_engines() -> None:
    b = use(FakeSource(BARS)).get(SERIES, params={"range": "1y"}).json()
    tail = BARS.iloc[-252:]
    ind = IndicatorEngine().analyze(BARS[["open", "high", "low", "close", "volume"]]).values
    assert b["close"] == pytest.approx(tail["close"].tolist())
    assert b["overlays"]["sma_50"] == pytest.approx(ind["sma_50"].iloc[-252:].tolist())
    assert b["oscillators"]["rsi_14"] == pytest.approx(ind["rsi_14"].iloc[-252:].tolist())


def test_warm_up_values_are_null_never_zero() -> None:
    b = use(FakeSource(BARS)).get(SERIES, params={"range": "5y"}).json()
    sma200 = b["overlays"]["sma_200"]
    assert sma200[:199] == [None] * 199
    assert all(v is not None and v > 0 for v in sma200[199:])
    assert b["oscillators"]["macd"][0] is None


def test_series_is_causal_appending_bars_changes_nothing_before() -> None:
    short = use(FakeSource(BARS.iloc[:400])).get(SERIES, params={"range": "1y"}).json()
    m.CACHE.clear()
    full = use(FakeSource(BARS)).get(SERIES, params={"range": "5y"}).json()
    pos = {s: i for i, s in enumerate(full["sessions"])}
    idx = [pos[s] for s in short["sessions"]]
    for group in ("overlays", "oscillators"):
        for k, vals in short[group].items():
            assert vals == [full[group][k][i] for i in idx], k


# ── engine reuse (phase 3C): identical results, one Price Action run ──


def _same(a: Any, b: Any) -> bool:
    va, vb = vars(a), vars(b)
    return va.keys() == vb.keys() and all(
        va[k].equals(vb[k]) if hasattr(va[k], "equals") else va[k] == vb[k] for k in va
    )


def test_compute_engines_equals_independent_engine_runs() -> None:
    from app.candles.engine import CandlestickEngine
    from app.price_action.engine import PriceActionEngine
    from app.regimes.engine import RegimeEngine

    o = BARS[["open", "high", "low", "close", "volume"]]
    e = m.compute_engines(BARS)
    assert _same(e.candles, CandlestickEngine().analyze(o))
    assert _same(e.price_action, PriceActionEngine().analyze(o))
    assert _same(e.indicators, IndicatorEngine().analyze(o))
    assert _same(e.regimes, RegimeEngine().analyze(o))  # state, version and fingerprint


def test_price_action_engine_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.price_action.engine import PriceActionEngine

    calls = []
    original = PriceActionEngine.analyze

    def counting(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PriceActionEngine, "analyze", counting)
    m.compute_engines(BARS)
    assert len(calls) == 1  # previously 2 (once more inside the regime engine)


def test_precomputed_result_is_reused_only_for_the_same_input_and_config() -> None:
    from app.price_action.config import PriceActionConfig
    from app.price_action.engine import PriceActionEngine

    o = BARS[["open", "high", "low", "close", "volume"]]
    engine = PriceActionEngine()
    result = engine.analyze(o)
    proxy = m._Precomputed(engine, o, result)
    assert proxy.analyze(o.copy()) is result and proxy.reused == 1
    other = o.iloc[:-1]
    assert _same(proxy.analyze(other), engine.analyze(other))  # different bars -> computed
    assert proxy.analyze(o, instrument_id=1) is not result  # different call -> computed
    changed = m._Precomputed(PriceActionEngine(PriceActionConfig(swing_left_bars=4)), o, result)
    assert changed.analyze(o) is not result and changed.reused == 0  # different config
    assert proxy.reused == 1


# ── value cleaning ──


@pytest.mark.parametrize(
    "v", [None, float("nan"), float("inf"), -float("inf"), np.nan, pd.NA, pd.NaT, "x"]
)
def test_num_never_turns_missing_into_a_number(v: Any) -> None:
    assert m.num(v) is None


def test_num_keeps_finite_values_including_zero() -> None:
    assert m.num(0) == 0.0 and m.num(np.float64(1.5)) == 1.5 and m.num(np.int64(3)) == 3.0


def test_non_finite_engine_values_serialise_as_null() -> None:
    bars = BARS.copy()
    engines = m.compute_engines(bars)
    engines.indicators.values.loc[bars.index[-1], "sma_50"] = math.inf
    first, last = bars.index[0], bars.index[-1]
    ident = DatasetIdentity("SPY", 1, "yfinance", "1d", N, first, last, last)
    out = m.build_series(m.Loaded(ident, None, engines), NOW, "3m")
    assert out.overlays["sma_50"][-1] is None
    assert "Infinity" not in out.model_dump_json()


# ── errors and parameters ──


@pytest.mark.parametrize("exc", [NoDataError("none"), LookupError("instrument 'SPY' not found")])
def test_no_stored_data_is_404(exc: Exception) -> None:
    for path in (SUMMARY, SERIES):
        r = use(RaisingSource(exc)).get(path)
        assert (r.status_code, r.json()["detail"]) == (404, "no stored market data")


def test_unreachable_database_is_503_without_details() -> None:
    dead = create_engine(
        "postgresql+psycopg://nobody:nopass@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    app.dependency_overrides[get_engine] = lambda: dead  # the real DbMarketSource path
    client = TestClient(app)
    for path in (SUMMARY, SERIES):
        r = client.get(path)
        assert (r.status_code, r.json()) == (503, {"detail": "database unavailable"})
        assert "nopass" not in r.text and "127.0.0.1" not in r.text


@pytest.mark.parametrize(
    ("path", "query"),
    [
        (SERIES, "range=10y"),
        (SERIES, "range="),
        (SERIES, "range=1y&symbol=AAPL"),
        (SERIES, "range=1y&as_of=2019-01-02"),
        (SUMMARY, "symbol=AAPL"),
        (SUMMARY, "as_of=2019-01-02"),
        (SUMMARY, "range=1y"),
    ],
)
def test_invalid_or_unknown_parameters_are_422(path: str, query: str) -> None:
    assert use(FakeSource(BARS)).get(f"{path}?{query}").status_code == 422


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", [SUMMARY, SERIES])
def test_write_methods_are_rejected(method: str, path: str) -> None:
    src = FakeSource(BARS)
    r = use(src).request(method, path, content=b"{}")
    assert r.status_code == 405
    assert src.computed == 0  # nothing ran


# ── cache ──


def test_endpoint_computes_engines_once_per_dataset() -> None:
    src = FakeSource(BARS)
    client = use(src)
    for path in (SUMMARY, SERIES, SUMMARY, f"{SERIES}?range=5y"):
        assert client.get(path).status_code == 200
    assert src.computed == 1
    src.ingested = pd.Timestamp("2020-01-02", tz="UTC")  # a new ingestion -> new dataset key
    assert client.get(SUMMARY).status_code == 200
    assert src.computed == 2


def test_cache_is_bounded_lru() -> None:
    c = m.AnalysisCache(maxsize=2)
    for k in ("a", "b", "a", "c"):
        c.get(k, lambda k=k: k.upper())  # type: ignore[misc]
    assert list(c._data) == ["a", "c"]  # "b" was least recently used
    assert (c.hits, c.misses, c.computations) == (1, 3, 3)


def test_cache_never_stores_failures() -> None:
    c = m.AnalysisCache()
    calls = []

    def boom() -> str:
        calls.append(1)
        raise RuntimeError("engine failed")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            c.get("k", boom)
    assert len(calls) == 2 and c.get("k", lambda: "ok") == "ok"


def test_concurrent_requests_compute_once() -> None:
    c = m.AnalysisCache()
    started = threading.Event()
    calls = []

    def slow() -> str:
        calls.append(1)
        started.set()
        time.sleep(0.2)
        return "value"

    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(c.get("k", slow))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert started.is_set() and len(calls) == 1 and results == ["value"] * 8


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
    ident = DatasetIdentity("SPY", 1, "yfinance", "1d", N, BARS.index[0], BARS.index[-1], NOW)

    def identity(s: Any, symbol: str) -> DatasetIdentity:
        RecordingSession.log.append(f"identity:{symbol}")
        return ident

    monkeypatch.setattr(m, "Session", RecordingSession)
    monkeypatch.setattr(m.market, "dataset_identity", identity)
    monkeypatch.setattr(m.market, "latest_ingestion", lambda s, i: None)
    monkeypatch.setattr(m.market, "load_bars", lambda s, i: BARS)
    loaded = m.DbMarketSource(object()).load(m.AnalysisCache())  # type: ignore[arg-type]
    assert RecordingSession.log == [
        "SET TRANSACTION READ ONLY",
        "identity:SPY",
        "rollback",
        "close",
    ]
    assert loaded.engines.bars is BARS


# ── import boundary ──

FORBIDDEN_PREFIXES = (
    "app.broker",
    "app.paper",
    "app.operations",
    "app.alerts",
    "app.data.ingestion",
    "app.data.providers",
    "app.dashboard.services.broker",
    "app.dashboard.services.paper",
    "app.dashboard.ui",
    "yfinance",
    "streamlit",
    "requests",
    "httpx",
)


def test_market_module_imports_nothing_operational() -> None:
    tree = ast.parse(Path(m.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    assert not [n for n in names if n.startswith(FORBIDDEN_PREFIXES)]


def test_importing_the_api_loads_no_operational_module() -> None:
    code = (
        "import json, sys; import app.api.main; "
        f"p = {FORBIDDEN_PREFIXES!r}; "
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith(p))))"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
