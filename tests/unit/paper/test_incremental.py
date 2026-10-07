"""Incremental, restart-safe paper trading: replay equivalence, idempotency, restarts, crash
recovery, corruption detection, ledger reconstruction, rebalances and exact P&L examples."""

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import session_times
from app.data.calendar import TradingCalendar
from app.indicators.engine import IndicatorEngine
from app.paper.config import PaperConfig
from app.paper.engine import PaperTradingEngine, build_inputs
from app.paper.ledger import LedgerError, reconstruct_snapshots
from app.paper.models import ADD, ENTRY, EXIT, REDUCE
from app.paper.state import StateError, dumps, loads, to_dict
from app.paper.store import DataRevisionError, PaperStore
from app.paper.trader import BarInput
from app.risk.config import SCENARIOS, RiskConfig, SizingSpec
from tests.unit.risk.test_overlay import make_bars
from tests.unit.strategies.helpers import session_walk, small_engine

CAL = TradingCalendar("XNYS")
OHLCV = ["open", "high", "low", "close", "volume"]
VOL_CFG = PaperConfig(risk=SCENARIOS[2])  # volatility target: partial rebalances happen


@pytest.fixture(scope="module")
def walk() -> dict[str, list[BarInput]]:
    bars = session_walk(260, seed=31)
    strat = small_engine().run(bars)
    ind = IndicatorEngine().analyze(bars[OHLCV]).values
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    return {
        str(sid): build_inputs(
            bars,
            pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"])),
            ind,
            times,
        )
        for sid, g in strat.signals.groupby("strategy_id", sort=False)
    }


def store(root: Path, cfg: PaperConfig = VOL_CFG, sid: str = "rsi_momentum") -> PaperStore:
    return PaperStore(cfg, strategy_id=sid, strategy_version="1.0.0", root=root)


def replay(inputs: list[BarInput], cfg: PaperConfig = VOL_CFG, sid: str = "rsi_momentum"):  # type: ignore[no-untyped-def]
    return PaperTradingEngine(cfg).replay_inputs(inputs, strategy_id=sid)


# ── equivalence ──


@pytest.mark.parametrize("sid", ["rsi_momentum", "sma_trend", "buy_and_hold"])
def test_bar_by_bar_with_restarts_equals_replay(walk, tmp_path: Path, sid: str) -> None:  # type: ignore[no-untyped-def]
    inputs = walk[sid]
    ref = replay(inputs, sid=sid)
    s = store(tmp_path, sid=sid)
    for i, bar in enumerate(inputs):
        if i and i % 37 == 0:  # restart from disk regularly
            s = store(tmp_path, sid=sid)
        assert s.process(bar).status == "processed"
    s = store(tmp_path, sid=sid)
    events = s.events()
    assert events == ref.events  # every record, ID and hash identical
    assert to_dict(s.trader.state) == to_dict(ref.final_state)
    snaps = pd.DataFrame([e["record"] for e in events if e["event_type"] == "snapshot"])
    p = ref.portfolio
    for col in (
        "cash",
        "position_quantity",
        "equity",
        "realized_pnl",
        "unrealized_pnl",
        "total_pnl",
        "cumulative_costs",
        "cost_basis",
    ):
        np.testing.assert_array_equal(snaps[col].to_numpy(), p[col].to_numpy())
    fills = [e["record"] for e in events if e["event_type"] == "fill"]
    assert [f["fill_id"] for f in fills] == ref.fills["fill_id"].tolist()
    assert [f["notional"] for f in fills] == ref.fills["notional"].tolist()


def test_batch_and_resume_equals_single_pass(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    inputs = walk["sma_crossover"]
    a = store(tmp_path / "a", sid="sma_crossover")
    a.process_many(inputs[:120])
    a = store(tmp_path / "a", sid="sma_crossover")
    counts = a.process_many(inputs)  # whole history again: first 120 already processed
    assert counts == {"processed": len(inputs) - 120, "already_processed": 120, "recovered": 0}
    b = store(tmp_path / "b", sid="sma_crossover")
    b.process_many(inputs)
    assert a.events() == b.events()
    assert a.state_path.read_text() == b.state_path.read_text()  # deterministic bytes


# ── idempotency / restart ──


def test_processing_the_same_bar_twice_has_no_effect(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    inputs = walk["rsi_momentum"]
    s = store(tmp_path)
    s.process_many(inputs[:80])
    state_before = s.state_path.read_bytes()
    ledger_before = s.ledger_path.read_bytes()
    assert s.process(inputs[79]).status == "already_processed"
    assert s.process(inputs[10]).status == "already_processed"
    assert s.state_path.read_bytes() == state_before
    assert s.ledger_path.read_bytes() == ledger_before

    s2 = store(tmp_path)  # restart, then the same bar again
    assert s2.process(inputs[79]).status == "already_processed"
    assert s2.state_path.read_bytes() == state_before
    assert s2.ledger_path.read_bytes() == ledger_before
    assert s2.process(inputs[80]).status == "processed"


def test_restart_restores_complete_state(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    inputs = walk["rsi_momentum"]
    s = store(tmp_path)
    s.process_many(inputs[:150])
    st = s.trader.state
    r = store(tmp_path).trader.state
    assert to_dict(r) == to_dict(st)
    assert r.pending == st.pending and r.open_trade == st.open_trade
    assert vars(r.account) == vars(st.account)
    assert (r.risk.dd.hwm, r.risk.dd.state, r.risk.current_target) == (
        st.risk.dd.hwm,
        st.risk.dd.state,
        st.risk.current_target,
    )


def test_state_serialisation_round_trip_is_exact(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    s = store(tmp_path, cfg=PaperConfig(risk=SCENARIOS[4]))  # ATR stop: NaN/finite stops
    s.process_many(walk["rsi_momentum"][:200])
    text = s.state_path.read_text()
    state, pointer = loads(text, PaperConfig(risk=SCENARIOS[4]))
    assert dumps(state, pointer) == text


# ── crash recovery and corruption ──


def _crash_after_ledger_append(s: PaperStore, bar: BarInput, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom() -> None:
        raise OSError("simulated crash before the state file was written")

    monkeypatch.setattr(s, "_write_state", boom)
    with pytest.raises(OSError, match="simulated crash"):
        s.process(bar)
    with pytest.raises(RuntimeError, match="reopen"):
        s.process(bar)  # the object is unusable after a failed commit


def test_crash_between_ledger_and_state_is_recovered_without_duplicates(
    walk, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    inputs = walk["rsi_momentum"]
    s = store(tmp_path)
    s.process_many(inputs[:100])
    _crash_after_ledger_append(s, inputs[100], monkeypatch)
    reopened = store(tmp_path)
    assert reopened.status()["uncommitted_ledger_events"] > 0
    assert reopened.status()["sessions_processed"] == 100
    assert reopened.process(inputs[100]).status == "recovered"
    reopened.process_many(inputs[101:])
    assert reopened.events() == replay(inputs).events
    assert len(reopened._read_ledger()) == len(reopened.events())  # no duplicate events


def test_crash_then_different_input_is_refused(walk, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    inputs = walk["rsi_momentum"]
    s = store(tmp_path)
    s.process_many(inputs[:100])
    _crash_after_ledger_append(s, inputs[100], monkeypatch)
    reopened = store(tmp_path)
    changed = BarInput(**{**asdict(inputs[100]), "close": inputs[100].close * 1.01})
    with pytest.raises(LedgerError, match="differ"):
        reopened.process(changed)


def test_edited_ledger_is_detected(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    s = store(tmp_path)
    s.process_many(walk["rsi_momentum"][:60])
    lines = s.ledger_path.read_text().splitlines()
    ev = json.loads(lines[10])
    ev["record"] = {**ev["record"], "risk_state": "TAMPERED"}
    lines[10] = json.dumps(ev, sort_keys=True)
    s.ledger_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerError, match="hash chain"):
        store(tmp_path)


def test_missing_ledger_events_are_detected(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    s = store(tmp_path)
    s.process_many(walk["rsi_momentum"][:60])
    lines = s.ledger_path.read_text().splitlines()
    s.ledger_path.write_text("\n".join(lines[:-3]) + "\n")
    with pytest.raises(LedgerError, match="shorter"):
        store(tmp_path)


def test_corrupt_or_incompatible_state_is_refused(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    s = store(tmp_path)
    s.process_many(walk["rsi_momentum"][:30])
    good = s.state_path.read_text()
    s.state_path.write_text(good[:-20])
    with pytest.raises(StateError, match="not valid JSON"):
        store(tmp_path)
    data = json.loads(good)
    data["account"]["shares"] = -1.0
    s.state_path.write_text(json.dumps(data))
    with pytest.raises(StateError):
        store(tmp_path)
    data = json.loads(good)
    data["schema_version"] = 99
    s.state_path.write_text(json.dumps(data))
    with pytest.raises(StateError, match="schema version"):
        store(tmp_path)
    s.state_path.write_text(good)
    other = PaperConfig(risk=SCENARIOS[2], cash_tolerance=1e-5)  # different config -> new id
    assert store(tmp_path, cfg=other).account_id != s.account_id
    with pytest.raises(StateError, match="different configuration"):
        loads(good, other)


def test_revised_data_for_a_processed_session_is_refused(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    inputs = walk["rsi_momentum"]
    s = store(tmp_path)
    s.process_many(inputs[:50])
    revised = BarInput(**{**asdict(inputs[49]), "close": inputs[49].close + 0.01})
    with pytest.raises(DataRevisionError):
        s.process(revised)


def test_future_mutation_never_changes_the_committed_ledger(walk, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    inputs = walk["rsi_momentum"]
    cut = 150
    mutated = inputs[: cut + 1] + [
        BarInput(
            **{
                **asdict(b),
                "open": b.open * 1.5,
                "low": b.low * 1.5,
                "close": b.close * 1.5,
                "strategy_state": "FLAT",
            }
        )
        for b in inputs[cut + 1 :]
    ]
    a, b = store(tmp_path / "a"), store(tmp_path / "b")
    a.process_many(inputs)
    b.process_many(mutated)
    through = [e for e in a.events() if pd.Timestamp(e["session"]) <= inputs[cut].bar_ts]
    assert b.events()[: len(through)] == through


# ── ledger reconstruction ──


@pytest.mark.parametrize("risk", [SCENARIOS[0], SCENARIOS[2], SCENARIOS[5]], ids=lambda r: r.name)
def test_snapshots_are_reproducible_from_the_ledger(walk, risk) -> None:  # type: ignore[no-untyped-def]
    cfg = PaperConfig(risk=risk)
    run = replay(walk["rsi_momentum"], cfg=cfg)
    rebuilt = pd.DataFrame(reconstruct_snapshots(run.events, cfg.backtest.initial_capital))
    for col in rebuilt.columns:
        np.testing.assert_array_equal(rebuilt[col].to_numpy(), run.portfolio[col].to_numpy())


# ── rebalancing: exact synthetic example ──

FREE = BacktestConfig(
    initial_capital=10_000.0, commission_per_trade=0.0, spread_bps=0.0, slippage_bps=0.0
)
VT = RiskConfig("vt_test", sizing=SizingSpec("volatility_target", target_volatility=0.12))


def _rebalance_inputs() -> list[BarInput]:
    """Targets decided at closes 0..4: 0.5, 0.8, 0.3, FLAT, FLAT (target 0.12 / vol)."""
    bars = make_bars(
        [
            (100, 100, 100, 100),
            (100, 100, 100, 100),
            (100, 100, 100, 100),
            (120, 120, 120, 120),
            (110, 110, 110, 110),
        ]
    )
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    states = pd.Series(["LONG", "LONG", "LONG", "FLAT", "FLAT"], index=bars.index)
    ind = pd.DataFrame(
        {"realized_vol_20": [0.24, 0.15, 0.40, 0.2, 0.2], "atr_14": 2.0}, index=bars.index
    )
    return build_inputs(bars, states, ind, times)


def test_rebalance_sequence_exact_accounting() -> None:
    cfg = PaperConfig(risk=VT, backtest=FREE)
    run = PaperTradingEngine(cfg).replay_inputs(_rebalance_inputs(), strategy_id="x")
    d, f, p = run.decisions, run.fills, run.portfolio
    assert d["risk_approved_target"].tolist() == pytest.approx([0.5, 0.8, 0.3, 0.0, 0.0])
    assert d["pending_kind"].tolist() == ["entry", "rebalance", "rebalance", "exit", "none"]
    assert f["action"].tolist() == [ENTRY, ADD, REDUCE, EXIT]
    # t1 open 100: buy 5,000 (50 sh); t2 open 100: buy ~3,000 (to 80 sh)
    # t3 open 120: equity 11,600 -> target 0.3 = 3,480 -> sell 6,120 (51 sh): realized 1,020
    # t4 open 110: sell the remaining 29 sh = 3,190 against basis 2,900: realized 290
    assert f["quantity"].tolist() == pytest.approx([50, 30, -51, -29])
    assert f["realized_pnl"].tolist() == pytest.approx([0, 0, 1020, 290])
    assert p["realized_pnl"].tolist() == pytest.approx([0, 0, 0, 1020, 1310])
    assert p["cost_basis"].tolist() == pytest.approx([0, 5000, 8000, 2900, 0])
    assert p["unrealized_pnl"].tolist() == pytest.approx([0, 0, 0, 29 * 120 - 2900, 0])
    assert p["equity"].iloc[-1] == pytest.approx(11_310) == p["cash"].iloc[-1]
    assert run.trades["net_pnl"].tolist() == pytest.approx([1310])  # one round trip, 4 fills
    assert run.trades["fills"].tolist() == [4] and len(run.trades) == 1
    assert (p["gross_exposure"] <= 1).all() and (p["position_quantity"] >= 0).all()


def test_final_bar_rebalance_is_pending() -> None:
    cfg = PaperConfig(risk=VT, backtest=FREE)
    run = PaperTradingEngine(cfg).replay_inputs(_rebalance_inputs()[:2], strategy_id="x")
    assert run.pending_order is not None and run.pending_order["kind"] == "rebalance"
    assert run.pending_order["risk_approved_target"] == pytest.approx(0.8)
    assert run.portfolio["pending_kind"].iloc[-1] == "rebalance"
