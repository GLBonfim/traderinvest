"""Phase 15 on real SPY data with a FAKE gateway (no network, no external order): the sandbox
order derived from a real RiskManager-approved paper decision is exact, deterministic,
idempotent and reconciles with the local simulation."""

import math
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.backtest.data import load_backtest_bars
from app.broker.config import BrokerConfig
from app.broker.executor import SandboxExecutor, derive_intent
from app.broker.reconcile import reconcile
from app.broker.store import BrokerStore
from app.core.config import Settings, TradingMode
from app.dashboard.services import paper as paper_svc
from app.paper.engine import PaperTradingEngine, baseline_inputs
from tests.unit.broker.test_broker import FakeGateway

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def spy_inputs():  # type: ignore[no-untyped-def]
    try:
        engine = create_engine(Settings().database_url(), connect_args={"connect_timeout": 3})  # type: ignore[call-arg]
        with Session(engine) as s:
            _, bars = load_backtest_bars(s, "SPY")
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(bars) < 8000:
        pytest.skip("full SPY history required")
    return baseline_inputs(bars)


def paper_settings() -> Settings:
    s = Settings(_env_file=None, postgres_password="x")  # type: ignore[call-arg]
    object.__setattr__(s, "trading_mode", TradingMode.PAPER)
    return s


def test_sandbox_order_from_a_real_pending_entry(spy_inputs, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    version, inputs = spy_inputs["sma_trend"]
    recent = [b for b in inputs if b.bar_ts.year >= 2025]
    replay = PaperTradingEngine(paper_svc.paper_config("control_no_overlay")).replay_inputs(
        recent, strategy_id="sma_trend", strategy_version=version
    )
    entries = replay.decisions[replay.decisions["pending_kind"] == "entry"]
    assert len(entries), "no entry decision in the sample"
    cut = pd.Timestamp(entries["bar_ts"].iloc[0])
    upto = [b for b in recent if b.bar_ts <= cut]
    store = paper_svc.paper_create_account(
        tmp_path / "paper",
        strategy_id="sma_trend",
        strategy_version=version,
        scenario_name="control_no_overlay",
        instrument="SPY",
    )
    store.process_many(upto)
    now = upto[-1].observed_at.to_pydatetime() + timedelta(minutes=45)  # before the next open
    cfg = BrokerConfig()

    intent = derive_intent(store.dir, cfg, now)
    assert intent is not None and intent.pending_kind == "entry"
    d = entries.iloc[0]
    assert (
        intent.decision_id == d["decision_id"]
        and intent.approved_exposure == d["risk_approved_target"]
    )
    assert intent.reference_price == upto[-1].close
    assert intent.target_qty == math.floor(
        d["risk_approved_target"] * cfg.allocated_capital / upto[-1].close
    )
    assert pd.Timestamp(intent.scheduled_for) == upto[-1].effective_at
    assert intent.client_order_id == f"qp-{store.account_id}-{d['decision_id'].split('-')[-1]}"

    bstore = BrokerStore(tmp_path / "broker")
    bstore.link(store.account_id)
    gw = FakeGateway()
    ex = SandboxExecutor(cfg, bstore, paper_settings(), tmp_path / "paper", gw, clock=lambda: now)
    assert [i.outcome for i in ex.sync(dry_run=True).items] == ["blocked"]  # unarmed by default
    bstore.set_armed(True)
    dry = ex.sync(dry_run=True)
    assert dry.items[0].outcome == "would_submit" and gw.submits == []
    rep = ex.sync(dry_run=False)
    assert rep.items[0].outcome == "submitted" and len(gw.submits) == 1
    req = gw.submits[0]
    assert (req.side, req.qty, req.time_in_force, req.symbol) == (
        "buy",
        intent.target_qty,
        "opg",
        "SPY",
    )
    assert ex.sync(dry_run=False).items[0].outcome == "already_submitted" and len(gw.submits) == 1

    # after the simulated fill at the next open, the sandbox (assumed filled) reconciles
    nxt = [b for b in recent if b.bar_ts > cut][:1]
    store.process_many(nxt)
    gw.positions["SPY"] = req.qty
    rec = reconcile(cfg, bstore, gw, tmp_path / "paper", now + timedelta(days=1))  # type: ignore[arg-type]
    assert rec.accounts[0]["status"] == "ok", rec.accounts
