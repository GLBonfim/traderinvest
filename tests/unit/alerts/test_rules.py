"""Every alert rule: exact conditions, severities, payloads, transitions only, neutral wording."""

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from app.alerts import rules
from app.alerts.models import CRITICAL, EVENT_TYPES, INFO, PAPER_TAG, WARNING, alert_id_for
from app.risk.config import SCENARIOS, RiskConfig, SizingSpec
from tests.unit.alerts.helpers import paper_run

TS = pd.DatetimeIndex(pd.date_range("2024-07-01 13:30", periods=6, freq="B", tz="UTC"))
OBS = pd.Series(TS + pd.Timedelta(hours=6, minutes=30), index=TS)
FORBIDDEN = (
    "buy spy",
    "sell spy",
    "you should",
    "we recommend",
    "bullish",
    "bearish",
    "opportunity",
    "will rise",
    "will fall",
)


def regime_frame(**cols: list[str]) -> pd.DataFrame:
    base = {
        "trend_regime": ["uptrend"] * 6,
        "volatility_regime": ["normal"] * 6,
        "momentum_regime": ["neutral"] * 6,
        "composite_regime": ["transition"] * 6,
    }
    base.update(cols)
    f = pd.DataFrame(base, index=TS)
    for c in (
        "trend_quality",
        "volatility_measure",
        "volatility_percentile",
        "atr_pct",
        "momentum_rsi",
        "momentum_roc",
        "momentum_macd",
    ):
        f[c] = np.linspace(0.1, 0.6, 6)
    for d in ("trend", "volatility", "momentum", "composite"):
        f[f"{d}_age"] = 1
    return f


def test_regime_change_fires_once_per_transition_only() -> None:
    f = regime_frame(volatility_regime=["normal", "normal", "high", "extreme", "extreme", "high"])
    out = rules.regime_alerts(f, OBS, instrument="SPY")
    vol = [a for a in out if a.event_type == "REGIME_VOLATILITY_CHANGED"]
    assert [(a.payload["previous"], a.payload["current"]) for a in vol] == [
        ("normal", "high"),
        ("high", "extreme"),
        ("extreme", "high"),
    ]
    ext = [a for a in out if a.event_type == "REGIME_VOLATILITY_EXTREME"]
    assert len(ext) == 1 and ext[0].severity == WARNING and ext[0].session == "2024-07-04"
    assert ext[0].payload["volatility_percentile"] == f["volatility_percentile"].iloc[3]
    assert ext[0].occurred_at == OBS.iloc[3].isoformat()  # the close of the session
    assert all(a.severity == INFO for a in vol)
    assert not [
        a
        for a in out
        if a.event_type != "REGIME_VOLATILITY_CHANGED"
        and a.event_type != "REGIME_VOLATILITY_EXTREME"
    ]
    for a in out:
        text = f"{a.title} {a.message}".lower()
        assert not any(w in text for w in FORBIDDEN)
        assert "not a prediction" in a.message or "observed conditions" in a.message


def test_regime_first_row_and_since_filter() -> None:
    f = regime_frame(
        trend_regime=["insufficient_data", "uptrend", "uptrend", "range", "range", "range"]
    )
    assert len(rules.regime_alerts(f, OBS, instrument="SPY")) == 2
    since = rules.regime_alerts(f, OBS, instrument="SPY", since=TS[2])
    assert [a.payload["current"] for a in since] == ["range"]


def strategy_signals(states: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "strategy_id": "sma_trend",
            "strategy_version": "1.0.0",
            "bar_ts": TS,
            "observed_at": OBS.to_numpy(),
            "effective_at": TS + pd.Timedelta(days=1),
            "state": states,
            "reason": "close>sma",
        }
    )


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (
            ["INSUFFICIENT_DATA", "LONG", "LONG", "FLAT", "FLAT", "LONG"],
            [("INSUFFICIENT_DATA", "LONG"), ("LONG", "FLAT"), ("FLAT", "LONG")],
        ),
        (
            ["INSUFFICIENT_DATA", "FLAT", "FLAT", "FLAT", "FLAT", "FLAT"],
            [("INSUFFICIENT_DATA", "FLAT")],
        ),
        (["LONG"] * 6, []),
        (
            ["LONG", "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", "LONG", "LONG", "LONG"],
            [("INSUFFICIENT_DATA", "LONG")],
        ),  # LONG -> INSUFFICIENT_DATA is not an alert type
    ],
)
def test_strategy_transitions(states: list[str], expected: list[tuple[str, str]]) -> None:
    out = rules.strategy_alerts(strategy_signals(states), instrument="SPY")
    assert [(a.payload["previous"], a.payload["current"]) for a in out] == expected
    assert all(a.event_type == "STRATEGY_STATE_CHANGED" and a.severity == INFO for a in out)
    assert all("not a recommendation" in a.message for a in out)


def test_alert_ids_are_point_in_time_and_versionless() -> None:
    f = regime_frame(
        momentum_regime=["neutral", "positive", "positive", "positive", "neutral", "neutral"]
    )
    a = rules.regime_alerts(f, OBS, instrument="SPY", app_version="0.13.0")
    b = rules.regime_alerts(f, OBS, instrument="SPY", app_version="9.9.9")
    assert [x.alert_id for x in a] == [x.alert_id for x in b]
    assert a[0].dedup_key == "REGIME_MOMENTUM_CHANGED|SPY|2024-07-02|neutral|positive"
    assert a[0].alert_id == alert_id_for(a[0].dedup_key)


# ── paper / risk ──

VT = RiskConfig("vt", sizing=SizingSpec("volatility_target", target_volatility=0.1))


def _paper_alerts(run, since=None):  # type: ignore[no-untyped-def]
    return rules.paper_alerts(
        run.events,
        account_id=run.account_id,
        strategy_id="x",
        instrument="SPY",
        initial_capital=10_000.0,
        since=since,
    )


def test_paper_scheduling_fills_and_risk_reduction() -> None:
    run = paper_run(
        [100.0] * 12,
        [
            "FLAT",
            "LONG",
            "LONG",
            "LONG",
            "FLAT",
            "FLAT",
            "LONG",
            "LONG",
            "FLAT",
            "FLAT",
            "FLAT",
            "FLAT",
        ],
        VT,
    )
    out = _paper_alerts(run)
    by = lambda t: [a for a in out if a.event_type == t]  # noqa: E731
    fills = run.fills
    assert len(by("PAPER_FILL_EXECUTED")) == len(fills) == 4
    assert [a.payload["fill_id"] for a in by("PAPER_FILL_EXECUTED")] == fills["fill_id"].tolist()
    kinds = run.decisions["pending_kind"]
    assert len(by("PAPER_ENTRY_SCHEDULED")) == int((kinds == "entry").sum()) == 2
    assert len(by("PAPER_EXIT_SCHEDULED")) == int((kinds == "exit").sum()) == 2
    # vol target 0.1 / 0.2 = 0.5: reduced once per entry episode, not every LONG session
    red = by("RISK_EXPOSURE_REDUCED")
    assert len(red) == 2 and all(a.payload["approved_exposure"] == 0.5 for a in red)
    for a in out:
        assert a.title.startswith(PAPER_TAG) and PAPER_TAG in a.message
        assert "brokerage" not in a.title.lower() or "not a brokerage" in a.message.lower()
    assert all(a.account_id == run.account_id for a in out)


def test_paper_rebalance_and_since() -> None:
    run = paper_run([100.0] * 8, ["FLAT"] + ["LONG"] * 7, VT, vol=0.2)
    assert not [a for a in _paper_alerts(run) if a.event_type == "PAPER_REBALANCE_SCHEDULED"]
    from app.paper.config import PaperConfig
    from app.paper.engine import PaperTradingEngine
    from tests.unit.paper.test_incremental import FREE as PFREE
    from tests.unit.paper.test_incremental import VT as PVT
    from tests.unit.paper.test_incremental import _rebalance_inputs

    r2 = PaperTradingEngine(PaperConfig(risk=PVT, backtest=PFREE)).replay_inputs(
        _rebalance_inputs(), strategy_id="x"
    )
    out = _paper_alerts(r2)
    assert len([a for a in out if a.event_type == "PAPER_REBALANCE_SCHEDULED"]) == 2
    assert {a.payload["action"] for a in out if a.event_type == "PAPER_FILL_EXECUTED"} == {
        "entry",
        "add",
        "reduce",
        "exit",
    }
    cut = pd.Timestamp(r2.decisions["bar_ts"].iloc[3])
    later = _paper_alerts(r2, since=cut)
    assert later and all(pd.Timestamp(a.occurred_at) >= cut for a in later)


def test_full_block_drawdown_lock_and_cooldown_end() -> None:
    risk = SCENARIOS[3]  # drawdown lock at 20%, force flat, cooldown 21 sessions
    closes = [100.0, 100.0, 95.0, 88.0, 80.0, 75.0, 74.0] + [76.0] * 30
    run = paper_run(closes, ["LONG"] * len(closes), risk)
    out = _paper_alerts(run)
    types = [a.event_type for a in out]
    assert "RISK_DRAWDOWN_WARNING" in types
    start = [a for a in out if a.event_type == "RISK_LOCK_COOLDOWN_STARTED"]
    end = [a for a in out if a.event_type == "RISK_LOCK_COOLDOWN_ENDED"]
    blocked = [a for a in out if a.event_type == "RISK_EXPOSURE_BLOCKED"]
    assert (
        len(start) == 1
        and len(end) == 1
        and pd.Timestamp(end[0].occurred_at) > pd.Timestamp(start[0].occurred_at)
    )
    assert len(blocked) == 1  # once when blocking starts, not every locked session
    assert blocked[0].payload["approved_exposure"] == 0.0
    assert "drawdown_lock_flat" in blocked[0].payload["reasons"]


def test_stop_triggered_and_rejected_order() -> None:
    run = paper_run([100.0, 100.0, 100.0, 92.0, 92.0, 92.0], ["LONG"] * 6, SCENARIOS[4], atr=2.0)
    stops = [a for a in _paper_alerts(run) if a.event_type == "RISK_STOP_TRIGGERED"]
    assert len(stops) == 1 and stops[0].severity == WARNING
    from app.backtest.engine import session_times
    from app.paper.engine import PaperTradingEngine
    from tests.unit.risk.test_overlay import flat_indicators, make_bars

    full = make_bars([(100, 101, 99, 100)] * 8, start=date(2024, 7, 8))
    bars = full.drop(full.index[2])
    times = session_times(pd.DatetimeIndex(bars.index), rules_cal())
    st = pd.Series(["FLAT"] + ["LONG"] * 6, index=bars.index)
    r = PaperTradingEngine().replay(bars, st, flat_indicators(bars), times, strategy_id="x")
    rej = [a for a in _paper_alerts(r) if a.event_type == "PAPER_ORDER_REJECTED"]
    assert (
        len(rej) == 1 and rej[0].payload["rejection_reason"] == "scheduled_session_missing_in_data"
    )


def rules_cal():  # type: ignore[no-untyped-def]
    from app.data.calendar import TradingCalendar

    return TradingCalendar("XNYS")


def test_snapshot_safety_invariants() -> None:
    common = {"account_id": "a", "instrument": "SPY", "strategy_id": "x", "app_version": ""}
    good = {
        "snapshot_id": "a-S1",
        "valued_at": "2024-07-01T20:00:00+00:00",
        "cash": 0.0,
        "position_quantity": 1.0,
        "gross_exposure": 1.0,
        "total_pnl": 5.0,
        "equity": 105.0,
    }
    assert rules._snapshot_safety(good, "2024-07-01", 100.0, common) == []
    bad = {
        **good,
        "cash": -1.0,
        "position_quantity": -1.0,
        "gross_exposure": 1.01,
        "total_pnl": 9.0,
    }
    out = rules._snapshot_safety(bad, "2024-07-01", 100.0, common)
    assert {a.event_type for a in out} == {
        "SAFETY_NEGATIVE_CASH",
        "SAFETY_NEGATIVE_POSITION",
        "SAFETY_EXPOSURE_ABOVE_LIMIT",
        "SAFETY_ACCOUNTING_RECONCILIATION_FAILED",
    }
    assert all(a.severity == CRITICAL and "No automatic action" in a.message for a in out)


# ── data ──


def test_missing_session_stale_ingestion_and_quality_rules() -> None:
    expected = pd.DatetimeIndex(
        rules_cal().sessions(date(2024, 7, 1), date(2024, 7, 12))["open_utc"]
    )
    stored = expected.delete([3, 6])
    out = rules.missing_session_alerts(stored, expected, instrument="SPY")
    assert [a.session for a in out] == ["2024-07-05", "2024-07-10"]
    assert (
        rules.stale_data_alert(
            instrument="SPY",
            last_bar_session=date(2024, 7, 12),
            expected_session=date(2024, 7, 12),
            missing_sessions=0,
            detected_at="x",
        )
        is None
    )
    s1 = rules.stale_data_alert(
        instrument="SPY",
        last_bar_session=date(2024, 7, 10),
        expected_session=date(2024, 7, 12),
        missing_sessions=2,
        detected_at="2024-07-12T21:00:00+00:00",
    )
    s2 = rules.stale_data_alert(
        instrument="SPY",
        last_bar_session=date(2024, 7, 10),
        expected_session=date(2024, 7, 15),
        missing_sessions=3,
        detected_at="2024-07-15T21:00:00+00:00",
    )
    assert s1 is not None and s2 is not None and s1.alert_id == s2.alert_id  # one per episode
    now = datetime(2024, 7, 12, tzinfo=UTC)
    runs = [
        {
            "run_id": 7,
            "provider": "yfinance",
            "status": "failed",
            "error": "timeout",
            "started_at": now,
            "finished_at": now,
        },
        {
            "run_id": 8,
            "provider": "yfinance",
            "status": "succeeded",
            "error": None,
            "started_at": now,
            "finished_at": now,
        },
    ]
    ing = rules.ingestion_alerts(runs, instrument="SPY")
    assert [a.payload["run_id"] for a in ing] == [7] and ing[0].severity == WARNING
    q = [
        {
            "id": 1,
            "check_name": "provider_failure",
            "severity": "critical",
            "description": "d.",
            "action_taken": "ingestion_aborted",
            "detected_at": now,
            "bar_ts": None,
            "ingestion_run_id": 7,
            "details": {},
        },
        {
            "id": 2,
            "check_name": "return_outlier",
            "severity": "warning",
            "description": "d.",
            "action_taken": "kept_flagged",
            "detected_at": now,
            "bar_ts": now,
            "ingestion_run_id": 8,
            "details": {},
        },
    ]
    qa = rules.quality_event_alerts(q, instrument="SPY")
    assert [(a.event_type, a.severity) for a in qa] == [
        ("DATA_PROVIDER_FAILURE", CRITICAL),
        ("DATA_QUALITY_EVENT", WARNING),
    ]


def test_catalog_severities_are_the_three_levels() -> None:
    assert {s for s, _ in EVENT_TYPES.values()} == {INFO, WARNING, CRITICAL}
    assert all(
        t.startswith(
            ("DATA_", "REGIME_", "STRATEGY_", "RISK_", "PAPER_", "SYSTEM_", "SAFETY_", "TEST_")
        )
        for t in EVENT_TYPES
    )
