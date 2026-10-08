"""Phase 14.5 on real SPY data in the throwaway test database (no network: the provider serves
bars read from the local development database).

Scenario: the system is current through T-3 (2026-10-01); operations then process T-2, T-1, T
(2026-10-02, 2026-10-05, 2026-10-06) session by session. Paper ledgers must equal the standalone
Phase 12 tools fed with the same bars; alerts, history and health must be consistent; crashes
between stages and provider failures must not duplicate any financial effect.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.alerts.channels import ConsoleChannel
from app.alerts.store import AlertStore
from app.backtest.data import load_backtest_bars
from app.core.config import Settings
from app.dashboard.services import paper as paper_svc
from app.data.calendar import TradingCalendar
from app.data.ingestion import ingest_daily_bars
from app.data.instruments import get_spec
from app.data.providers.yfinance_provider import YFinanceProvider
from app.database.models import IngestionRun, PriceBar
from app.operations import models as m
from app.operations.config import OperationsConfig
from app.operations.pipeline import Pipeline
from app.operations.services import SystemServices
from app.operations.store import OpsStore
from app.paper.engine import baseline_inputs
from app.paper.store import PaperStore

pytestmark = pytest.mark.integration
T3, T2, T1, T = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # T closed 20:00 UTC; grace 30 min
START = date(2025, 10, 1)
ACCOUNTS = (("sma_trend", "volatility_target_10"), ("rsi_momentum", "control_no_overlay"))
CAL = TradingCalendar("XNYS")


class Crash(BaseException):
    pass


@pytest.fixture(scope="module")
def dev_bars() -> pd.DataFrame:
    try:
        engine = create_engine(Settings().database_url(), connect_args={"connect_timeout": 3})  # type: ignore[call-arg]
        with Session(engine) as s:
            _, bars = load_backtest_bars(s, "SPY")
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(bars) < 8000 or bars.index[-1].date() < T:
        pytest.skip("full SPY history through 2026-10-06 required")
    return bars


class Feed:
    """yfinance-shaped history from local bars; only dates <= `published` exist; can fail."""

    def __init__(self, bars: pd.DataFrame) -> None:
        self.bars = bars
        self.published = T
        self.fail_on: set[date] = set()

    def __call__(self, symbol: str, start: date, end_excl: date) -> pd.DataFrame:
        if start in self.fail_on:
            raise ConnectionError("provider unreachable (injected)")
        idx = self.bars.index.tz_convert("America/New_York")
        sel = self.bars[(idx.date >= start) & (idx.date < end_excl) & (idx.date <= self.published)]
        out = pd.DataFrame(
            {
                "Open": sel["open"],
                "High": sel["high"],
                "Low": sel["low"],
                "Close": sel["close"],
                "Adj Close": sel["adj_close"],
                "Volume": sel["volume"],
                "Dividends": 0.0,
                "Stock Splits": 0.0,
                "Capital Gains": 0.0,
            }
        )
        out.index = pd.DatetimeIndex(
            [pd.Timestamp(d.date()) for d in sel.index.tz_convert("America/New_York")], name="Date"
        ).tz_localize("America/New_York")
        return out


@dataclass
class Env:
    engine: Engine
    feed: Feed
    root: Path
    dev: pd.DataFrame
    reference: dict[tuple[str, str, date], list[dict]] = field(default_factory=dict)  # type: ignore[type-arg]

    def pipeline(self, cfg: OperationsConfig | None = None, now: datetime = NOW) -> Pipeline:
        cfg = cfg or OperationsConfig()
        svc = SystemServices(
            cfg,
            engine=self.engine,
            settings=Settings(),  # type: ignore[call-arg]
            paper_root=self.root / "paper",
            alerts_root=self.root / "alerts",
            ops_root=self.root / "ops",
            provider=YFinanceProvider(self.feed),
            calendar=CAL,
            channels=[ConsoleChannel()],
        )
        return Pipeline(cfg, svc, OpsStore(self.root / "ops"), clock=lambda: now, calendar=CAL)

    def stores(self) -> dict[str, PaperStore]:
        return {
            r.strategy_id: paper_svc.open_account(r, self.root / "paper")
            for r in paper_svc.list_accounts(self.root / "paper")
        }

    def bar_count(self) -> int:
        with Session(self.engine) as s:
            return int(s.scalar(select(func.count(PriceBar.id))) or 0)


@pytest.fixture
def env(migrated_engine: Engine, dev_bars: pd.DataFrame, tmp_path: Path) -> Iterator[Env]:
    with migrated_engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE data_quality_events, ingestion_runs, price_bars, market_sessions, "
                "provider_symbols, instruments RESTART IDENTITY CASCADE"
            )
        )
    feed = Feed(dev_bars)
    feed.published = T3
    with Session(migrated_engine) as s:  # system state at T-3
        ingest_daily_bars(
            s,
            YFinanceProvider(feed),
            get_spec("SPY"),
            date(1993, 1, 1),
            T3,
            calendar=CAL,
            as_of=datetime(2026, 10, 1, 21, 0, tzinfo=UTC),
        )
        _, bars = load_backtest_bars(s, "SPY")
    inputs = baseline_inputs(bars)
    for sid, scen in ACCOUNTS:
        version, b = inputs[sid]
        st = paper_svc.paper_create_account(
            tmp_path / "paper",
            strategy_id=sid,
            strategy_version=version,
            scenario_name=scen,
            instrument="SPY",
        )
        paper_svc.paper_process_sessions(st, b, START)
    feed.published = T
    yield Env(migrated_engine, feed, tmp_path, dev_bars)


def reference_events(env: Env, sid: str, scen: str, upto: date) -> list[dict]:  # type: ignore[type-arg]
    """Standalone Phase 12 tools fed with the same bars, independent of operations (built once
    per account and session, then reused)."""
    key = (sid, scen, upto)
    if key in env.reference:
        return env.reference[key]
    bars = env.dev[env.dev.index.tz_convert("America/New_York").date <= upto]
    version, b = baseline_inputs(bars)[sid]
    root = env.root / "reference" / f"{sid}-{upto}"
    st = paper_svc.paper_create_account(
        root, strategy_id=sid, strategy_version=version, scenario_name=scen, instrument="SPY"
    )
    paper_svc.paper_process_sessions(st, b, START)
    env.reference[key] = st.events()
    return env.reference[key]


def assert_ledgers_match_reference(env: Env, upto: date) -> None:
    stores = env.stores()
    for sid, scen in ACCOUNTS:
        assert stores[sid].events() == reference_events(env, sid, scen, upto), sid


def test_catch_up_matches_standalone_tools_and_is_idempotent(env: Env) -> None:
    dry = env.pipeline().run(mode="dry_run")
    assert dry.target_sessions == ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]
    assert env.bar_count() == len(env.dev) - 3 and not (env.root / "ops" / "runs.jsonl").exists()

    r = env.pipeline().run()
    assert r.status == m.COMPLETED, (r.errors, r.warnings)
    assert [s.status for s in r.sessions] == [m.SESSION_COMPLETED] * 4
    assert r.sessions[0].stages[0].status == m.SKIPPED  # T-3 already stored: verification only
    assert [s.stages[0].result.get("bars_inserted") for s in r.sessions[1:]] == [1, 1, 1]
    assert env.bar_count() == len(env.dev)
    with Session(env.engine) as s:
        stored = s.execute(
            select(PriceBar.ts, PriceBar.close).order_by(PriceBar.ts.desc()).limit(3)
        ).all()
    assert [float(c) for _, c in stored][::-1] == env.dev["close"].iloc[-3:].tolist()
    assert_ledgers_match_reference(env, T)
    for st in env.stores().values():
        assert st.trader.state.last_session.date() == T  # type: ignore[union-attr]

    alerts = AlertStore(env.root / "alerts").alerts()
    fills = [
        e["record"]["fill_id"]
        for st in env.stores().values()
        for e in st.events()
        if e["event_type"] == "fill" and pd.Timestamp(e["session"]).date() >= T3
    ]
    got = [a["payload"]["fill_id"] for a in alerts if a["event_type"] == "PAPER_FILL_EXECUTED"]
    assert sorted(got) == sorted(fills)
    assert len({a["alert_id"] for a in alerts}) == len(alerts)

    hist = OpsStore(env.root / "ops").history()
    assert len(hist) == 1 and hist[0]["status"] == m.COMPLETED
    assert list(hist[0]["sessions"]) == ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]
    assert r.health is not None and r.health.result["overall"] == "ok"
    comps = r.health.result["components"]
    assert {
        "database",
        "schema",
        "market_data",
        "ingestion",
        "paper_accounts",
        "alert_store",
        "safety",
        "application",
        "pipeline",
    } <= set(comps)

    n_alerts, n_bars = len(alerts), env.bar_count()
    again = env.pipeline().run()
    assert again.status == m.NO_NEW_COMPLETED_SESSION
    assert (env.bar_count(), len(AlertStore(env.root / "alerts").alerts())) == (n_bars, n_alerts)
    assert len(OpsStore(env.root / "ops").history()) == 1
    assert_ledgers_match_reference(env, T)


def test_late_data_waits_then_completes(env: Env) -> None:
    env.feed.published = T1  # the provider has not published T yet
    r = env.pipeline().run()
    assert r.status == m.WAITING_FOR_DATA
    assert [s.status for s in r.sessions][-1] == m.SESSION_WAITING_FOR_DATA
    for st in env.stores().values():
        assert st.trader.state.last_session.date() == T1  # type: ignore[union-attr]
    env.feed.published = T
    r2 = env.pipeline(now=datetime(2026, 10, 6, 21, 40, tzinfo=UTC)).run()
    assert r2.status == m.COMPLETED and r2.target_sessions == ["2026-10-06"]
    assert_ledgers_match_reference(env, T)


def test_provider_failure_is_an_ingestion_failure(env: Env) -> None:
    env.feed.fail_on = {T2}
    r = env.pipeline().run()
    assert r.status == m.RUN_FAILED
    assert r.sessions[-1].session == "2026-10-02" and r.sessions[-1].stages[0].stage == m.INGEST
    for st in env.stores().values():
        assert st.trader.state.last_session.date() == T3  # type: ignore[union-attr]
    with Session(env.engine) as s:
        assert s.scalar(select(func.count()).where(IngestionRun.status == "failed")) == 1
    ops_alerts = [
        a for a in AlertStore(env.root / "alerts").alerts() if a["event_type"] == "OPS_STAGE_FAILED"
    ]
    assert len(ops_alerts) == 1 and ops_alerts[0]["payload"]["stage"] == m.INGEST
    env.feed.fail_on = set()
    assert env.pipeline().run().status == m.COMPLETED
    assert_ledgers_match_reference(env, T)


@pytest.mark.parametrize(("where", "session"), [("paper", T2), ("alerts", T1)])
def test_crash_between_stages_has_no_duplicate_effects(
    env: Env, monkeypatch: pytest.MonkeyPatch, where: str, session: date
) -> None:
    original = getattr(SystemServices, where)
    state = {"armed": True}

    def crashing(self, s, *a):  # type: ignore[no-untyped-def]
        if s == session and state["armed"]:
            if where == "alerts":
                original(self, s, *a)  # paper + alerts committed, then crash
            state["armed"] = False
            raise Crash(f"crash in {where} {s}")
        return original(self, s, *a)

    monkeypatch.setattr(SystemServices, where, crashing)
    with pytest.raises(Crash):
        env.pipeline().run()
    assert not (env.root / "ops" / "pipeline.lock").exists()
    assert OpsStore(env.root / "ops").history()[0]["status"] == "INTERRUPTED"
    r = env.pipeline().run()  # restart
    assert r.status == m.COMPLETED
    assert env.bar_count() == len(env.dev)  # no duplicate bars
    with Session(env.engine) as s:
        inserted = s.scalar(select(func.sum(IngestionRun.bars_inserted)))
    assert inserted == len(env.dev)  # each bar inserted exactly once
    assert_ledgers_match_reference(env, T)
    alerts = AlertStore(env.root / "alerts").alerts()
    assert len({a["alert_id"] for a in alerts}) == len(alerts)
