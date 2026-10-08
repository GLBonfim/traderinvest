"""Alert runs end to end (no database): point-in-time identity (prefix/full, future mutation),
idempotency and restart, condition episodes, corrupt paper accounts, delivery isolation from
paper state, and the safety boundary of the alert package."""

import ast
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.alerts.engine import market_alerts, run_alerts
from app.alerts.models import EVENT_TYPES
from app.alerts.store import AlertStore
from app.core.config import Settings, TradingMode
from app.core.safety import RealMoneyExecutionBlockedError, refuse_real_money_order
from app.dashboard.services import paper as paper_svc
from app.paper.engine import baseline_inputs
from tests.unit.alerts.helpers import fake_market, no_db_events, walk

REPO = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 7, 21, 0, tzinfo=UTC)


def settings(mode: str = "disabled") -> Settings:
    s = Settings(_env_file=None, postgres_password="x")  # type: ignore[call-arg]
    object.__setattr__(s, "trading_mode", mode)
    return s


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    return walk()


# ── point-in-time ──


def _through(alerts, ts):  # type: ignore[no-untyped-def]
    return sorted(
        (a for a in alerts if pd.Timestamp(a.occurred_at) <= ts + pd.Timedelta(hours=8)),
        key=lambda a: a.alert_id,
    )


def test_prefix_equals_full_history(bars: pd.DataFrame) -> None:
    full = market_alerts(bars, instrument="SPY", since=None)
    for cut in (280, 350, 400):
        part = market_alerts(bars.iloc[: cut + 1], instrument="SPY", since=None)
        assert _through(full, bars.index[cut]) == _through(part, bars.index[cut])


def test_future_mutation_never_changes_past_alerts(bars: pd.DataFrame) -> None:
    cut = 330
    base = market_alerts(bars, instrument="SPY", since=None)
    mut = bars.copy()
    mut.iloc[cut + 1 :, :4] *= 0.6
    mut.iloc[cut + 1 :, 4] = 1.0
    after = market_alerts(mut, instrument="SPY", since=None)
    assert _through(base, bars.index[cut]) == _through(after, bars.index[cut])
    assert {a.alert_id for a in base} != {a.alert_id for a in after}  # the future did change


# ── runs: idempotency, restart, episodes ──


def run(tmp: Path, bars: pd.DataFrame, now: datetime = NOW, **kw):  # type: ignore[no-untyped-def]
    return run_alerts(
        now=now,
        alerts_root=tmp / "alerts",
        paper_root=tmp / "paper",
        settings=kw.pop("settings", settings()),
        load_market=kw.pop("load_market", fake_market(bars, now)),
        load_db_events=no_db_events,
        deliver=kw.pop("deliver", True),
        since=kw.pop("since", date(2019, 6, 1)),
        **kw,
    )


def test_run_is_idempotent_across_restarts(tmp_path: Path, bars: pd.DataFrame) -> None:
    r1 = run(tmp_path, bars)
    assert r1.new_alerts and r1.delivery is not None
    assert r1.delivery.delivered == len(r1.new_alerts)
    r2 = run(tmp_path, bars)  # refresh / reprocess
    assert r2.new_alerts == [] and r2.delivery is not None and r2.delivery.attempted == 0
    store = AlertStore(tmp_path / "alerts")
    assert len(store.alerts()) == len(r1.new_alerts)
    assert len({a["alert_id"] for a in store.alerts()}) == len(store.alerts())
    assert store.since == date(2019, 6, 1)
    assert all(a["session"] is None or a["session"] >= "2019-06-01" for a in store.alerts())


def test_appending_bars_only_adds_alerts(tmp_path: Path, bars: pd.DataFrame) -> None:
    run(tmp_path, bars.iloc[:380])
    first = {a["alert_id"]: a for a in AlertStore(tmp_path / "alerts").alerts()}
    run(tmp_path, bars)
    later = {a["alert_id"]: a for a in AlertStore(tmp_path / "alerts").alerts()}
    assert set(first) <= set(later)
    assert all(later[k] == first[k] for k in first)  # nothing renamed or rewritten


def test_database_outage_is_one_episode(tmp_path: Path, bars: pd.DataFrame) -> None:
    def down():  # type: ignore[no-untyped-def]
        raise ConnectionError("db down")

    a = run(tmp_path, bars, load_market=down)
    b = run(tmp_path, bars, now=NOW + timedelta(minutes=5), load_market=down)
    types = lambda r: [x.event_type for x in r.new_alerts]  # noqa: E731
    assert types(a) == ["SYSTEM_DATABASE_UNAVAILABLE"] and types(b) == []
    assert a.health["database"]["status"] == "critical"
    run(tmp_path, bars, now=NOW + timedelta(minutes=10))  # recovered: condition cleared
    c = run(tmp_path, bars, now=NOW + timedelta(minutes=15), load_market=down)
    assert types(c) == ["SYSTEM_DATABASE_UNAVAILABLE"]  # a new episode, a new alert


def test_unsupported_trading_mode_alert(tmp_path: Path, bars: pd.DataFrame) -> None:
    r = run(tmp_path, bars, settings=settings("live"))
    assert "SYSTEM_UNSUPPORTED_TRADING_MODE" in [a.event_type for a in r.new_alerts]
    assert r.health["safety"]["status"] == "critical"
    assert not [a for a in run(tmp_path, bars, settings=settings("live")).new_alerts]


def test_stale_data_alert_once_per_episode(tmp_path: Path, bars: pd.DataFrame) -> None:
    later = NOW + timedelta(days=3650)  # synthetic bars end years before
    r1 = run(tmp_path, bars, now=later)
    r2 = run(tmp_path, bars, now=later + timedelta(days=1))
    assert [a.event_type for a in r1.new_alerts].count("DATA_STALE") == 1
    assert "DATA_STALE" not in [a.event_type for a in r2.new_alerts]


# ── paper accounts ──


def _account(root: Path, bars: pd.DataFrame) -> Path:
    version, inputs = baseline_inputs(bars)["rsi_momentum"]
    st = paper_svc.paper_create_account(
        root,
        strategy_id="rsi_momentum",
        strategy_version=version,
        scenario_name="volatility_target_10",
        instrument="SPY",
    )
    paper_svc.paper_process_sessions(st, inputs)
    return st.dir


def test_paper_alerts_match_the_ledger_and_never_change_it(
    tmp_path: Path, bars: pd.DataFrame
) -> None:
    acct = _account(tmp_path / "paper", bars)
    digest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in acct.iterdir()}
    r = run(tmp_path, bars, since=date(2018, 1, 1))
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in acct.iterdir()} == digest
    ledger = [json.loads(x) for x in (acct / "ledger.jsonl").read_text().splitlines()]
    fills = [e for e in ledger if e["event_type"] == "fill"]
    got = [a for a in r.new_alerts if a.event_type == "PAPER_FILL_EXECUTED"]
    assert [a.payload["fill_id"] for a in got] == [f["record"]["fill_id"] for f in fills]
    assert all(a.account_id == acct.name and "PAPER SIMULATION" in a.title for a in got)


@pytest.mark.parametrize(
    ("damage", "etype"),
    [("ledger", "PAPER_LEDGER_VERIFICATION_FAILED"), ("state", "PAPER_STATE_CORRUPT")],
)
def test_corrupt_paper_account_alerts_once(
    tmp_path: Path, bars: pd.DataFrame, damage: str, etype: str
) -> None:
    acct = _account(tmp_path / "paper", bars)
    target = acct / ("ledger.jsonl" if damage == "ledger" else "state.json")
    text = target.read_text()
    target.write_text(
        text.replace('"cash"', '"cosh"', 1)
        if damage == "state"
        else text.replace("rsi_momentum", "tampered", 1)
    )
    before = target.read_bytes()
    r1 = run(tmp_path, bars)
    r2 = run(tmp_path, bars, now=NOW + timedelta(hours=1))
    assert [a.event_type for a in r1.new_alerts].count(etype) == 1
    assert etype not in [a.event_type for a in r2.new_alerts]
    assert target.read_bytes() == before  # nothing repaired
    assert r1.health["paper_accounts"]["status"] == "critical"


# ── safety boundary ──

TRADING = {
    "app.paper.broker",
    "app.paper.store",
    "app.paper.trader",
    "app.paper.engine",
    "app.risk.manager",
    "app.risk.engine",
    "app.backtest.portfolio",
}
NETWORK = {
    "requests",
    "httpx",
    "httpx2",
    "socket",
    "http",
    "aiohttp",
    "websocket",
    "websockets",
    "grpc",
    "urllib3",
    "smtplib",
    "telegram",
    "discord",
}
VENDORS = {
    "alpaca",
    "alpaca_trade_api",
    "ib_insync",
    "ib_async",
    "ibapi",
    "ccxt",
    "anthropic",
    "openai",
    "google",
}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_alerts_cannot_trade_and_only_the_webhook_touches_the_network() -> None:
    files = sorted((REPO / "app" / "alerts").glob("*.py"))
    assert len(files) >= 8
    for f in files:
        imps = _imports(f)
        assert not imps & TRADING, f
        roots = {i.split(".")[0] for i in imps}
        assert not roots & (NETWORK | VENDORS), f
        if f.name != "channels.py":
            assert not any(i.startswith("urllib") for i in imps), f
    src = "\n".join(f.read_text(encoding="utf-8") for f in files)
    for forbidden in (
        "PaperBroker",
        "process_many",
        "paper_process_sessions",
        "paper_create_account",
        "place_order",
        "submit_order",
    ):
        assert forbidden not in src, forbidden


def test_trading_modes_and_blocker_unchanged() -> None:
    assert {m.value for m in TradingMode} == {"disabled", "paper"}
    with pytest.raises(RealMoneyExecutionBlockedError):
        refuse_real_money_order()
    assert not [t for t in EVENT_TYPES if t.startswith(("ORDER_", "LIVE_", "BUY", "SELL"))]
