"""Isolated synthetic M5 product replay, never broker qualification."""

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.fx.config import FxConfig, RiskSettings, SimulationSettings
from app.fx.environment import Environment
from app.fx.proposals import AgentRole
from app.fx.research.replay import run_replay


def replay(path, risk_pct=0.1, approved=frozenset({"trend"})):
    index = pd.date_range("2026-10-19T00:00:00Z", periods=360, freq="5min")
    prices = pd.DataFrame(
        {
            f"{side}_{field}": np.full(len(index), 1.1 + spread + offset)
            for side, spread in (("bid", 0.0), ("ask", 0.00006))
            for field, offset in (("open", 0.0), ("close", 0.0), ("high", 0.0002), ("low", -0.0002))
        },
        index=index,
    )
    signals = pd.DataFrame(
        {"dir": 0, "stop_dist": 0.003, "target_dist": 0.006, "score": 0.9, "max_hold": 2},
        index=index,
    )
    signals.iloc[300, signals.columns.get_loc("dir")] = -1
    feats = pd.DataFrame({"atr": 0.0015, "er": 0.3, "rsi": 40.0, "tradable": True}, index=index)
    regimes = pd.DataFrame({"regime": "trend", "veto": False}, index=index)
    cfg = FxConfig(
        pairs=["EURUSD"],
        timeframe="M5",
        risk=RiskSettings(
            risk_per_trade_pct=risk_pct,
            daily_loss_limit_pct=1.0,
            max_drawdown_pct=5.0,
            max_open_positions=3,
            max_leverage=3.0,
        ),
        simulation=SimulationSettings(
            commission_per_million=20.0, slippage_pips=0.0, warm_up_minutes=0
        ),
    )
    return run_replay(
        {"EURUSD": prices},
        index[300].to_pydatetime(),
        index[306].to_pydatetime(),
        {AgentRole.TREND: {"EURUSD": signals}},
        {AgentRole.TREND: 2},
        {"EURUSD": feats},
        {"EURUSD": regimes},
        approved,
        work_dir=path,
        timeframe="M5",
        config_override=cfg,
        max_order_units=5000,
        commission_min_usd=2.0,
    )


def test_m5_config_simulation_only_and_defaults_unchanged():
    assert FxConfig().timeframe == "M15"
    assert FxConfig(timeframe="M5").environment is Environment.SIMULATION
    with pytest.raises(ValidationError, match="SIMULATION"):
        FxConfig(timeframe="M5", environment=Environment.DEMO)


def test_m5_real_engine_minimum_commission_and_risk_settings(tmp_path):
    result = replay(tmp_path)
    assert result["entries_filled"] == result["positions_closed"] == 1
    assert result["largest_entry_units"] == 3000
    assert result["commissions_total"] == pytest.approx(4 / 1.10003)
    assert result["journal_chain_ok"]
    fills = pd.read_csv(result["artifacts"]["fills"])
    assert fills.commission.sum() == pytest.approx(result["commissions_total"])
    assert result["closed_positions_pnl"] == "gross_before_commissions"
    assert result["effective_config"]["risk"]["daily_loss_limit_pct"] == 1.0
    assert result["research_only"] and not result["qualified_for_paper"]
    assert not result["execution_enabled"] and not result["statistically_approved"]
    assert "financing_not_modelled" in result["limitations"]
    assert "assumed_intrabar_ohlc_path" in result["limitations"]


def test_m5_oversized_entry_rejected_instead_of_resized(tmp_path):
    result = replay(tmp_path, risk_pct=0.5)
    assert result["entries_filled"] == 0 and result["final_equity"] == 10_000
    assert result["journal_chain_ok"]


def test_m5_no_simulation_roles_no_entry(tmp_path):
    result = replay(tmp_path, approved=frozenset())
    assert result["entries_filled"] == 0 and result["commissions_total"] == 0


def test_replay_refuses_existing_journal(tmp_path):
    replay(tmp_path)
    with pytest.raises(ValueError, match="existing replay"):
        replay(tmp_path)
