"""Phase 14 on the real SPY dataset — the complete path
    SPY bar -> engines -> strategy -> risk -> paper -> alert
with alert timing checked against the actual domain events. No outcome is asserted."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError

from app.alerts.engine import run_alerts
from app.alerts.monitor import quality_events_since
from app.alerts.sources import dashboard_results_missing, load_market
from app.alerts.store import AlertStore
from app.backtest.engine import session_times
from app.core.config import Settings
from app.dashboard.services import paper as paper_svc
from app.data.calendar import TradingCalendar
from app.database.session import get_session_factory
from app.paper.engine import baseline_inputs
from app.regimes.engine import RegimeEngine
from app.strategies.engine import StrategyEngine

pytestmark = pytest.mark.integration
SINCE = date(2025, 10, 1)
OHLCV = ["open", "high", "low", "close", "volume"]


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    try:
        _, _, bars, _ = load_market()
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(bars) < 8000:
        pytest.skip("full SPY history required")
    root = tmp_path_factory.mktemp("alerts_spy")
    version, inputs = baseline_inputs(bars)["rsi_momentum"]
    store = paper_svc.paper_create_account(
        root / "paper", strategy_id="rsi_momentum", strategy_version=version,
        scenario_name="volatility_target_10", instrument="SPY",
    )  # fmt: skip
    paper_svc.paper_process_sessions(store, inputs, date(2025, 6, 2))
    now = datetime.now(UTC)
    res = run_alerts(
        now=now, settings=Settings(), alerts_root=root / "alerts",  # type: ignore[call-arg]
        paper_root=root / "paper", load_market=load_market,
        dashboard_results_missing=dashboard_results_missing, since=SINCE,
    )  # fmt: skip
    return bars, root, store, res


def test_alert_run_is_idempotent(setup) -> None:  # type: ignore[no-untyped-def]
    _, root, _, first = setup
    assert first.new_alerts and first.delivery.failed == 0
    again = run_alerts(settings=Settings(), alerts_root=root / "alerts",  # type: ignore[call-arg]
                       paper_root=root / "paper", load_market=load_market, since=SINCE)  # fmt: skip
    assert again.new_alerts == [] and again.delivery.attempted == 0
    store = AlertStore(root / "alerts", create=False)
    assert len({a["alert_id"] for a in store.alerts()}) == len(store.alerts())


def test_regime_and_strategy_alerts_match_the_engines(setup) -> None:  # type: ignore[no-untyped-def]
    bars, _, _, res = setup
    st = RegimeEngine().analyze(bars[OHLCV]).state
    obs = session_times(pd.DatetimeIndex(bars.index), TradingCalendar("XNYS"))["observed_at"]
    since_ts = pd.Timestamp(SINCE, tz="America/New_York").tz_convert("UTC")
    for dim in ("trend", "volatility", "momentum", "composite"):
        col = st[f"{dim}_regime"]
        prev = col.shift(1)
        expected = [(t, prev[t], col[t]) for t in col.index[(col != prev) & prev.notna()]
                    if t >= since_ts]  # fmt: skip
        got = [a for a in res.new_alerts if a.event_type == f"REGIME_{dim.upper()}_CHANGED"]
        assert [(a.payload["previous"], a.payload["current"]) for a in got] == [
            (p, c) for _, p, c in expected]  # fmt: skip
        assert [a.occurred_at for a in got] == [obs[t].isoformat() for t, _, _ in expected]
    sig = StrategyEngine().run(bars[OHLCV]).signals
    got = [a for a in res.new_alerts if a.event_type == "STRATEGY_STATE_CHANGED"]
    for a in got:
        g = sig[sig["strategy_id"] == a.strategy_id].set_index("bar_ts")
        ts = pd.Timestamp(a.payload["observed_at"])
        row = g[g["observed_at"] == ts].iloc[0]
        before = g["state"].shift(1)[g["observed_at"] == ts].iloc[0]
        assert (before, row["state"]) == (a.payload["previous"], a.payload["current"])


def test_paper_and_risk_alerts_follow_the_ledger_timing(setup) -> None:  # type: ignore[no-untyped-def]
    bars, _, store, res = setup
    events = store.events()
    decisions = {e["record"]["decision_id"]: e["record"] for e in events
                 if e["event_type"] == "decision"}  # fmt: skip
    fills = {e["record"]["fill_id"]: e["record"] for e in events if e["event_type"] == "fill"}
    since_ts = pd.Timestamp(SINCE, tz="America/New_York").tz_convert("UTC")
    fill_alerts = [a for a in res.new_alerts if a.event_type == "PAPER_FILL_EXECUTED"]
    assert [a.payload["fill_id"] for a in fill_alerts] == [
        k for k, f in fills.items() if pd.Timestamp(f["executed_at"]) >= since_ts]  # fmt: skip
    opens = set(pd.DatetimeIndex(bars.index))
    for a in fill_alerts:
        f = fills[a.payload["fill_id"]]
        assert a.occurred_at == f["executed_at"] and pd.Timestamp(f["executed_at"]) in opens
    for a in res.new_alerts:
        if a.event_type in ("PAPER_ENTRY_SCHEDULED", "PAPER_EXIT_SCHEDULED",
                            "PAPER_REBALANCE_SCHEDULED", "RISK_EXPOSURE_REDUCED",
                            "RISK_EXPOSURE_BLOCKED"):  # fmt: skip
            d = decisions[a.payload["decision_id"]]
            assert a.occurred_at == d["observed_at"]  # alert at the decision close
            assert pd.Timestamp(d["scheduled_execution_at"]) > pd.Timestamp(d["observed_at"])
            if a.event_type.startswith("RISK_"):
                assert d["risk_approved_target"] < d["strategy_requested_target"]
            else:
                assert a.event_type == f"PAPER_{d['pending_kind'].upper()}_SCHEDULED"
        if a.source in ("PaperTrading", "RiskManager"):
            assert "PAPER SIMULATION" in a.title and a.account_id == store.account_id


def test_data_alerts_and_health(setup) -> None:  # type: ignore[no-untyped-def]
    _, _, _, res = setup
    with get_session_factory()() as s:
        q = quality_events_since(s, SINCE)
    got = [a for a in res.new_alerts if a.event_type in ("DATA_QUALITY_EVENT",
                                                         "DATA_PROVIDER_FAILURE")]  # fmt: skip
    assert sorted(a.payload["event_id"] for a in got) == sorted(e["id"] for e in q)
    assert not [a for a in res.new_alerts if a.event_type == "DATA_MISSING_SESSION"]
    assert res.health["database"]["status"] == "ok"
    assert res.health["paper_accounts"]["status"] == "ok"
    assert res.health["safety"]["status"] == "ok"
