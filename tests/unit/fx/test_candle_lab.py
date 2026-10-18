"""Analyst handoff, causal context, explicit unapproved experiment and real paper guards."""

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.fx.ibkr.candle_lab import CandleLab, market_analysis, specialist
from app.fx.ibkr.pilot import closed_model_bars, mid_of
from tests.unit.fx.ibkr_fakes import NOW
from tests.unit.fx.test_pilot import m15, make, model, registry


def context() -> dict:
    return {
        "tradable": True,
        "trend": 1,
        "efficiency": 0.3,
        "atr": 0.001,
        "patterns": [
            {"name": "hammer", "family": "reversal", "direction": 1, "strength": 0.8},
            {
                "name": "three_white_soldiers",
                "family": "continuation",
                "direction": 1,
                "strength": 0.8,
            },
            {"name": "long_body", "family": "momentum", "direction": 1, "strength": 0.8},
        ],
    }


def test_analyst_ignores_future_candles() -> None:
    bars = closed_model_bars(m15(n=2300), "EURUSD", "H1", NOW)
    mid = mid_of(bars)
    cut = len(mid) - 10
    altered = mid.copy()
    altered.iloc[cut:] *= 1.4
    assert market_analysis(mid.iloc[:cut]) == market_analysis(altered.iloc[:cut])
    assert market_analysis(mid.iloc[:cut])["analyst"] == "analista-mercado-v1"


@pytest.mark.parametrize("role", ["reversao", "continuacao", "impulso"])
def test_three_specialists_consume_analyst_veto_and_pattern_family(role: str) -> None:
    mid = mid_of(closed_model_bars(m15(), "EURUSD", "H1", NOW))
    c = context()
    result = specialist(role, c, model(), mid)
    assert result is not None and int(result[0]["dir"]) == 1
    assert result[0]["stop_dist"] == 0.002 and result[0]["target_dist"] == 0.003
    c["tradable"] = False
    assert specialist(role, c, model(), mid) is None
    c["tradable"] = True
    c["patterns"] = []
    assert specialist(role, c, model(), mid) is None


def test_continuation_cannot_ignore_analyst_trend() -> None:
    mid = mid_of(closed_model_bars(m15(), "EURUSD", "H1", NOW))
    c = context()
    c["trend"] = -1
    assert specialist("continuacao", c, model(), mid) is None
    c["efficiency"] = 0.9
    assert specialist("reversao", c, model(), mid) is None


def test_handoff_is_journaled_before_paper_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.fx.ibkr import candle_lab

    models = [model("reversao"), model("continuacao"), model("impulso")]
    p, ib, _, clock = make(tmp_path, reg=registry(*models, active=3), armed=True)
    cohort = {
        "entry_until": (NOW + timedelta(days=7)).isoformat(),
        "agents": [{"role": m.model_id, "model": m.to_dict()} for m in models],
    }
    lab = CandleLab(
        p.ex,
        p.store,
        p.registry,
        p.fetch_bars,
        p.fetch_positions,
        cohort=cohort,
        config=p.config,
        armed=True,
        clock=clock,
        say=lambda _: None,
    )
    c = context()
    c["bar_close"] = pd.Timestamp(NOW).floor("h").isoformat()
    monkeypatch.setattr(candle_lab, "market_analysis", lambda _: c)
    asyncio.run(lab.cycle())
    assert len(ib.placed) == 3  # exactly one bracket, never three competing entries
    assert lab.store.recent(kinds=("pilot_market_analysis",))
    assert len(lab.store.recent(kinds=("pilot_specialist_proposal",))) >= 3
    assert len(lab.state.open) == 1
    assert p.registry["statistically_approved"] is False


def test_degraded_specialist_is_replaced_without_faking_historical_approval(tmp_path: Path) -> None:
    models = [model("current"), model("reserve")]
    reg = registry(*models)
    reg["ranking"] = [{"model_id": m.model_id, "paper_candidate": False} for m in models]
    p, _, _, clock = make(tmp_path, reg=reg)
    cohort = {
        "entry_until": (NOW + timedelta(days=7)).isoformat(),
        "agents": [{"role": "reversao", "model": m.to_dict()} for m in models],
    }
    lab = CandleLab(
        p.ex,
        p.store,
        reg,
        p.fetch_bars,
        p.fetch_positions,
        cohort=cohort,
        config=p.config,
        clock=clock,
    )
    lab.state.returns["current"] = [-1.0] * 5
    lab._retirement("current")
    assert lab.state.active_ids == ["reserve"]
    assert lab.state.retired_ids == ["current"]
    assert all(r["paper_candidate"] is False for r in reg["ranking"])
    lab.state.returns["reserve"] = [-1.0] * 5
    lab._retirement("reserve")
    assert lab.state.active_ids == []


def test_funded_executor_keeps_candle_side_and_requires_actual_eur_cash() -> None:
    import asyncio
    from types import SimpleNamespace

    from app.fx.ibkr.candle_lab import FundedPaperExecutor
    from app.fx.models import Side
    from app.fx.orders import ExecutionError
    from tests.unit.fx.ibkr_fakes import FACTORY, FakeIB, contract, ticker
    from tests.unit.fx.test_ibkr_execution import req

    ib = FakeIB()
    ib.tickers["EURUSD"] = ticker("EURUSD", 1.1, 1.10006)
    ib.accountValues = lambda: [
        SimpleNamespace(tag="$LEDGER-CashBalance", currency="EUR", value="1000000"),
        SimpleNamespace(tag="$LEDGER-CashBalance", currency="USD", value="1000"),
    ]
    ex = FundedPaperExecutor(
        ib,
        orders=FACTORY,
        contract_factory=contract,
        clock=lambda: NOW,
        timeout_s=0.05,
        poll_s=0.001,
    )
    with pytest.raises(ExecutionError, match="SELL"):
        asyncio.run(ex.place_bracket(req(side=Side.BUY)))
    assert ib.placed == []
    asyncio.run(ex.live_quotes(["EURUSD"]))
    result = asyncio.run(ex.place_bracket(req(side=Side.SELL)))
    assert result.filled_units == 5000
    assert len(ib.placed) == 3
    ib.accountValues = lambda: []
    with pytest.raises(ExecutionError, match="cash"):
        asyncio.run(ex.place_bracket(req("different", Side.SELL)))
    assert len(ib.placed) == 3


def test_funded_policy_does_not_hide_conflicting_specialist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.fx.ibkr import candle_lab

    models = [model("reversao"), model("continuacao"), model("impulso")]
    p, _, _, clock = make(tmp_path, reg=registry(*models, active=3))
    cohort = {
        "entry_until": (NOW + timedelta(days=7)).isoformat(),
        "agents": [{"role": m.model_id, "model": m.to_dict()} for m in models],
    }
    lab = CandleLab(
        p.ex,
        p.store,
        p.registry,
        p.fetch_bars,
        p.fetch_positions,
        cohort=cohort,
        config=p.config,
        funded_eur_only=True,
        clock=clock,
    )
    c = context()
    c["bar_close"] = pd.Timestamp(NOW).floor("h").isoformat()
    monkeypatch.setattr(candle_lab, "market_analysis", lambda _: c)

    def disagree(role, *_):
        return pd.Series({"dir": 1 if role == "reversao" else -1, "score": 0.8}), "test_pattern"

    monkeypatch.setattr(candle_lab, "specialist", disagree)
    assert asyncio.run(lab._signals(NOW)) == []
    assert lab.store.recent(kinds=("pilot_conflict",))


def test_unavailable_bar_is_not_downloaded_every_minute(tmp_path: Path) -> None:
    import asyncio

    p, _, _, clock = make(tmp_path)
    cohort = {
        "entry_until": (NOW + timedelta(days=7)).isoformat(),
        "agents": [{"role": "reversao", "model": model().to_dict()}],
    }
    calls = []

    async def missing(symbol, timeframe):
        calls.append(symbol)
        return m15(n=100)

    lab = CandleLab(
        p.ex,
        p.store,
        p.registry,
        missing,
        p.fetch_positions,
        cohort=cohort,
        config=p.config,
        clock=clock,
    )
    asyncio.run(lab._signals(NOW))
    first = len(calls)
    asyncio.run(lab._signals(NOW + timedelta(seconds=60)))
    assert first > 0 and len(calls) == first


def test_funding_baseline_is_subtracted_without_hiding_external_positions() -> None:
    import asyncio

    from app.fx.ibkr.candle_lab import positions_without_funding

    async def positions():
        return {"EURUSD": -1500.0, "EURJPY": -1000.0, "GBPUSD": 100.0}

    result = asyncio.run(
        positions_without_funding(positions, {"EURUSD": -1000.0, "EURJPY": -1000.0})()
    )
    assert result == {"EURUSD": -500.0, "EURJPY": 0.0, "GBPUSD": 100.0}


def test_disappearing_funding_position_is_ambiguous() -> None:
    import asyncio

    import pytest

    from app.fx.ibkr.candle_lab import positions_without_funding
    from app.fx.orders import ExecutionError

    async def missing():
        return {}

    with pytest.raises(ExecutionError) as caught:
        asyncio.run(positions_without_funding(missing, {"EURUSD": -1000.0})())
    assert caught.value.ambiguous
