"""Paper pilot: dry-run, arming, data/identity gates, protection, ambiguity, retirement."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.fx.agents.trained import FEATURE_NAMES, RUN_SCHEMA, LinearModel
from app.fx.ibkr import pilot as pilot_mod
from app.fx.ibkr.pilot import PaperPilot, PilotConfig, PilotStopError, closed_model_bars
from app.fx.store import FxStore
from tests.unit.fx.ibkr_fakes import NOW, FakeIB, executor, ticker

N = len(FEATURE_NAMES)


def model(mid: str = "ridge-H1-a", intercept: float = 1.0) -> LinearModel:
    """Always long (intercept above the threshold) wherever features are valid and liquid."""
    return LinearModel(
        mid,
        "ridge",
        "H1",
        (0.0,) * N,
        (1.0,) * N,
        (0.0,) * N,
        intercept,
        0.35,
        "2026-01-01T00:00:00+00:00",
    )


def registry(*models: LinearModel, active: int = 1) -> dict[str, Any]:
    return {
        "schema": RUN_SCHEMA,
        "statistically_approved": False,
        "models": [m.to_dict() for m in models],
        "active_model_ids": [m.model_id for m in models[:active]],
        "ranking": [{"model_id": m.model_id, "paper_candidate": True} for m in models],
    }


def m15(end: datetime = NOW, n: int = 2000) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(end).floor("15min"), periods=n, freq="15min", tz="UTC")
    t = np.arange(n)
    mid = 1.10 + 0.002 * np.sin(t / 40) + 0.0004 * np.sin(t / 3)
    out: dict[str, np.ndarray] = {}
    for side, off in (("bid", -0.00003), ("ask", 0.00003)):
        c = mid + off
        out |= {
            f"{side}_open": c,
            f"{side}_high": c + 0.0003,
            f"{side}_low": c - 0.0003,
            f"{side}_close": c,
        }
    return pd.DataFrame(out, index=idx)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.fetch_delay = timedelta(0)

    def __call__(self) -> datetime:
        return self.now


def make(
    tmp_path: Path,
    reg: dict[str, Any] | None = None,
    armed: bool = False,
    ib: FakeIB | None = None,
    clock: Clock | None = None,
) -> tuple[PaperPilot, FakeIB, list[str], Clock]:
    ib = ib or FakeIB()
    clock = clock or Clock()
    ib.tickers["EURUSD"] = ticker("EURUSD", 1.1000, 1.10006, ts=clock.now)
    said: list[str] = []
    fetched: list[str] = []

    async def bars(symbol: str, timeframe: str) -> pd.DataFrame:
        fetched.append(symbol)
        frame = m15(clock.now)
        clock.now += clock.fetch_delay  # history requests take time
        return frame

    async def positions() -> dict[str, float]:
        return dict(ib.positions)

    p = PaperPilot(
        executor(ib, clock),
        FxStore(tmp_path / "pilot.sqlite"),
        reg or registry(model()),
        bars,
        positions,
        PilotConfig(symbols=("EURUSD",)),
        armed=armed,
        stop_file=tmp_path / "STOP",
        clock=clock,
        say=said.append,
    )
    p.fetched = fetched  # type: ignore[attr-defined]
    return p, ib, said, clock


def cycle(p: PaperPilot) -> None:
    asyncio.run(p.cycle())


def kinds(p: PaperPilot) -> list[str]:
    return [e["kind"] for e in p.store.recent(500)]


def test_closed_bars_only() -> None:
    bars = closed_model_bars(m15(), "EURUSD", "H1", NOW)
    assert bars.index[-1] == pd.Timestamp("2026-10-21 13:00", tz="UTC")  # 14:00 bar still open


def test_dry_run_sends_nothing(tmp_path: Path) -> None:
    p, ib, said, _ = make(tmp_path)
    cycle(p)
    assert ib.placed == []
    assert "pilot_dry_run_entry" in kinds(p)
    assert any("[dry-run]" in s for s in said)


def test_armed_entry_is_protected_and_not_repeated(tmp_path: Path) -> None:
    p, ib, _, _ = make(tmp_path, armed=True)
    cycle(p)
    assert len(ib.placed) == 3
    assert "pilot_order_sent" in kinds(p) and "pilot_protected" in kinds(p)
    (t,) = p.state.open.values()
    assert t["units"] > 0 and t["model_id"] == "ridge-H1-a" and ib.positions["EURUSD"] == t["units"]
    assert p.risk.state.entries_used == 1
    cycle(p)  # same bar: no second order
    assert len(ib.placed) == 3


def test_no_candidates_means_no_trade(tmp_path: Path) -> None:
    p, ib, said, _ = make(tmp_path, reg=registry(model(), active=0), armed=True)
    cycle(p)
    assert ib.placed == [] and p.fetched == []  # type: ignore[attr-defined]
    assert any("NO TRADE" in s for s in said)


def test_market_closed_no_orders(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 10, 24, 12, 0, 30, tzinfo=NOW.tzinfo))  # Saturday
    p, ib, said, _ = make(tmp_path, armed=True, clock=clock)
    cycle(p)
    assert ib.placed == [] and any("mercado fechado" in s for s in said)


@pytest.mark.parametrize("bad", ["stale", "frozen", "no_time"])
def test_no_order_without_live_fresh_quote(tmp_path: Path, bad: str) -> None:
    p, ib, _, clock = make(tmp_path, armed=True)
    t = ib.tickers["EURUSD"]
    if bad == "stale":
        t.time = clock.now - timedelta(minutes=5)
    elif bad == "frozen":
        t.marketDataType = 2
    else:
        t.time = None
    cycle(p)
    assert ib.placed == []


def test_live_account_stops_the_pilot(tmp_path: Path) -> None:
    p, ib, _, _ = make(tmp_path, armed=True)
    ib.accounts = ["U1234567"]
    with pytest.raises(PilotStopError):
        cycle(p)
    assert ib.placed == [] and p.risk.state.kill_switch and not p.armed


def test_stop_file_disarms(tmp_path: Path) -> None:
    p, ib, _, _ = make(tmp_path, armed=True)
    (tmp_path / "STOP").write_text("")
    with pytest.raises(PilotStopError):
        cycle(p)
    assert ib.placed == [] and not p.armed


def test_read_only_gateway_stops_cleanly(tmp_path: Path) -> None:
    ib = FakeIB()
    ib.read_only = True
    p, ib, _, _ = make(tmp_path, armed=True, ib=ib)
    with pytest.raises(PilotStopError) as e:
        cycle(p)
    assert "Read-Only" in str(e.value) and not p.armed
    assert p.state.pending == {} and p.risk.state.reservations == {}


def test_unprotected_fill_raises_alarm(tmp_path: Path) -> None:
    ib = FakeIB()
    ib.child_status = "Inactive"
    p, ib, _, _ = make(tmp_path, armed=True, ib=ib)
    with pytest.raises(PilotStopError) as e:
        cycle(p)
    assert "sem stop/alvo" in str(e.value)
    assert p.risk.state.kill_switch and not p.armed and len(p.state.open) == 1


def test_ambiguous_entry_is_reconciled_never_resent(tmp_path: Path) -> None:
    ib = FakeIB()
    ib.fill_market = False
    p, ib, _, clock = make(tmp_path, armed=True, ib=ib)
    cycle(p)
    assert len(ib.placed) == 3 and len(p.state.pending) == 1
    for _ in range(3):
        clock.now += timedelta(seconds=30)
        ib.tickers["EURUSD"].time = clock.now
        cycle(p)
    assert len(ib.placed) == 3
    assert p.risk.state.kill_switch and "sem confirmação" in str(p.risk.state.kill_reason)


def test_ambiguous_entry_that_filled_is_adopted(tmp_path: Path) -> None:
    ib = FakeIB()
    ib.fill_market = False
    p, ib, _, _ = make(tmp_path, armed=True, ib=ib)
    cycle(p)
    parent = ib.by_ref(next(iter(p.state.pending)))
    ib._fill(parent, 1.10006)  # the answer arrives late
    cycle(p)
    assert p.state.pending == {} and len(p.state.open) == 1 and len(ib.placed) == 3


def test_foreign_position_on_pair_is_skipped(tmp_path: Path) -> None:
    ib = FakeIB()
    ib.positions["EURUSD"] = 25_000.0  # opened manually, not by the pilot
    p, ib, _, _ = make(tmp_path, armed=True, ib=ib)
    cycle(p)
    assert ib.placed == [] and "pilot_skip" in kinds(p)


def test_stop_hit_counted_once_and_retires_after_5r(tmp_path: Path) -> None:
    reg = registry(model("ridge-H1-a"), model("ridge-H1-b"), active=1)
    p, ib, _, clock = make(tmp_path, reg=reg, armed=True)
    p.state.returns["ridge-H1-a"] = [-1.0] * 4  # earlier paper closes
    cycle(p)
    (eid,) = p.state.open
    ib.hit(f"{eid}:sl")
    clock.now += timedelta(seconds=30)
    ib.tickers["EURUSD"].time = clock.now
    cycle(p)
    rets = p.state.returns["ridge-H1-a"]
    assert len(rets) == 5 and rets[-1] < -0.9  # about -1 R plus costs
    assert eid not in p.state.open and p.state.retired_ids == ["ridge-H1-a"]
    assert p.state.active_ids == ["ridge-H1-b"]
    # the replacement trades its own signal; the retired candidate never trades again
    assert {t["model_id"] for t in p.state.open.values()} <= {"ridge-H1-b"}
    assert "pilot_retire" in kinds(p)
    fills = list(p.state.seen_fills)
    cycle(p)  # the same fill is never counted twice
    assert len(p.state.returns["ridge-H1-a"]) == 5 and p.state.seen_fills == fills


def test_retired_without_replacement_is_no_trade(tmp_path: Path) -> None:
    p, ib, said, clock = make(tmp_path, armed=True)
    p.state.returns["ridge-H1-a"] = [-1.0] * 4
    cycle(p)
    (eid,) = p.state.open
    ib.hit(f"{eid}:sl")
    clock.now += timedelta(hours=1)
    ib.tickers["EURUSD"].time = clock.now
    n = len(ib.placed)
    cycle(p)
    assert p.state.active_ids == [] and len(ib.placed) == n
    assert any("NO TRADE" in s for s in said)


def test_time_stop_closes_at_market(tmp_path: Path) -> None:
    p, ib, _, clock = make(tmp_path, armed=True)
    cycle(p)
    (eid,) = p.state.open
    clock.now += timedelta(hours=10)  # beyond max_hold (8 H1 bars)
    ib.tickers["EURUSD"].time = clock.now
    cycle(p)
    assert eid in p.state.closed_entries
    assert ib.placed[3][1].orderRef == f"{eid}:x" and ib.positions["EURUSD"] == 0


def test_state_survives_restart_but_arming_does_not(tmp_path: Path) -> None:
    p, ib, _, clock = make(tmp_path, armed=True)
    cycle(p)
    p.store.close()
    p2, _, _, _ = make(tmp_path, ib=ib, clock=clock)
    assert p2.state.open == p.state.open and not p2.armed
    assert p2.risk.state.entries_used == 1


def test_cli_refuses_live_port_and_unconfirmed_arming(tmp_path: Path) -> None:
    sel = tmp_path / "selected.json"
    sel.write_text("{}")
    assert pilot_mod.main(["--port", "4001", "--selection", str(sel)]) == 3
    assert pilot_mod.main(["--port", "4002", "--arm", "--selection", str(sel)]) == 2


def test_registry_claiming_approval_is_refused(tmp_path: Path) -> None:
    reg = registry(model())
    reg["statistically_approved"] = True
    with pytest.raises(ValueError):
        make(tmp_path, reg=reg)


def test_protection_lost_later_raises_alarm(tmp_path: Path) -> None:
    p, ib, _, clock = make(tmp_path, armed=True)
    cycle(p)
    (eid,) = p.state.open
    ib.by_ref(f"{eid}:sl").orderStatus.status = "Cancelled"  # e.g. cancelled in TWS
    clock.now += timedelta(seconds=30)
    ib.tickers["EURUSD"].time = clock.now
    n = len(ib.placed)
    with pytest.raises(PilotStopError):
        cycle(p)
    assert p.risk.state.kill_switch and len(ib.placed) == n


def test_weekend_flatten_with_stop_race_never_reverses(tmp_path: Path) -> None:
    friday = datetime(2026, 10, 23, 14, 0, 30, tzinfo=NOW.tzinfo)
    clock = Clock(friday)
    p, ib, _, _ = make(tmp_path, armed=True, clock=clock)
    cycle(p)
    (eid,) = p.state.open
    # the stop executes while the pilot cancels the target to flatten before the weekend
    ib.on_cancel = lambda t: (
        ib.hit(f"{eid}:sl")
        if t.order.orderRef.endswith(":tp") and t.orderStatus.status == "Submitted"
        else None
    )
    clock.now = datetime(2026, 10, 23, 20, 50, 30, tzinfo=NOW.tzinfo)  # 16:50 New York
    ib.tickers["EURUSD"].time = clock.now
    n = len(ib.placed)
    cycle(p)
    assert len(ib.placed) == n and ib.positions["EURUSD"] == 0
    assert eid in p.state.closed_entries and p.state.pending == {}
    close = [e for e in p.store.recent(500) if e["kind"] == "pilot_close"]
    assert close and close[0]["payload"]["reason"] == "stop"


def test_slow_history_makes_quotes_stale_no_order(tmp_path: Path) -> None:
    p, ib, _, clock = make(tmp_path, armed=True)
    clock.fetch_delay = timedelta(seconds=40)  # the ticker timestamp does not move
    cycle(p)
    assert ib.placed == [] and "pilot_skip" in kinds(p)


def test_slow_history_makes_signal_too_old_no_order(tmp_path: Path) -> None:
    p, ib, _, clock = make(tmp_path, armed=True)
    clock.fetch_delay = timedelta(minutes=25)
    real_quotes = p.ex.live_quotes

    async def fresh(symbols: list[str]) -> Any:
        ib.tickers["EURUSD"].time = clock.now  # quotes are fresh, the bar is not
        return await real_quotes(symbols)

    p.ex.live_quotes = fresh  # type: ignore[method-assign]
    cycle(p)
    assert ib.placed == []
    skips = [e["payload"]["reason"] for e in p.store.recent(500) if e["kind"] == "pilot_skip"]
    assert any("sinal antigo" in r for r in skips)


def test_partial_entry_raises_alarm(tmp_path: Path) -> None:
    ib = FakeIB()
    ib.partial = 5_000
    p, ib, _, _ = make(tmp_path, armed=True, ib=ib)
    with pytest.raises(PilotStopError) as e:
        cycle(p)
    assert "parcial" in str(e.value) and p.risk.state.kill_switch and not p.armed
    (t,) = p.state.open.values()
    assert t["units"] == 5_000  # the real exposure is tracked


def test_self_check_offline_reports_no_trade(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    sel = tmp_path / "selected.json"
    sel.write_text(json.dumps(registry(model(), active=0)), encoding="utf-8")
    code = pilot_mod.main(["--self-check", "--selection", str(sel), "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "NENHUM candidato elegível" in out and "auto-verificação OK" in out


def test_frozen_defaults_use_bundle_and_app_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("TRADERINVEST_FX_HOME", str(tmp_path / "home"))
    assert pilot_mod.default_selection() == tmp_path / "pilot" / "selected.json"
    assert pilot_mod.default_dir() == tmp_path / "home" / "pilot"


def test_desktop_entry_dispatches_the_pilot(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.desktop import __main__ as desktop_main

    seen: list[list[str]] = []

    def fake_main(args: list[str]) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(pilot_mod, "main", fake_main)
    assert desktop_main.main(["--paper-pilot", "--self-check"]) == 0
    assert seen == [["--self-check"]]


def _partial_session(on_cancel: Any = None, ack: bool = True) -> FakeIB:
    ib = FakeIB()
    ib.partial = 5_000
    ib.cancel_ack = ack

    def hook(t: Any) -> None:
        if t.order.orderType == "MKT" and on_cancel is not None:
            on_cancel(ib, t)
        elif ack:
            t.orderStatus.status = "Cancelled"

    ib.on_cancel = hook
    return ib


def test_partial_parent_filling_more_during_cancel_records_final_total(tmp_path: Path) -> None:
    def more_then_cancelled(ib: FakeIB, t: Any) -> None:
        ib._fill(t, 1.10006, 10_000)  # extra fills arrive while the cancel is in flight
        t.orderStatus.status = "Cancelled"

    p, ib, _, _ = make(tmp_path, armed=True, ib=_partial_session(more_then_cancelled))
    with pytest.raises(PilotStopError) as e:
        cycle(p)
    (t,) = p.state.open.values()
    assert t["units"] == 15_000 == ib.positions["EURUSD"]  # the real final exposure
    assert "parcial" in str(e.value) and p.risk.state.kill_switch and p.state.pending == {}
    assert len(ib.placed) == 3  # only the bracket: no flatten sent


def test_partial_parent_completing_during_cancel_is_a_normal_entry(tmp_path: Path) -> None:
    def completes(ib: FakeIB, t: Any) -> None:
        ib._fill(t, 1.10006)  # the rest fills before the cancel lands

    p, ib, _, _ = make(tmp_path, armed=True, ib=_partial_session(completes))
    cycle(p)
    (t,) = p.state.open.values()
    assert abs(t["units"]) == ib.positions["EURUSD"] and not p.risk.state.kill_switch
    assert "pilot_protected" in kinds(p)


def test_partial_parent_cancel_without_ack_stays_pending(tmp_path: Path) -> None:
    p, ib, _, _ = make(tmp_path, armed=True, ib=_partial_session(lambda ib, t: None, ack=False))
    with pytest.raises(PilotStopError) as e:
        cycle(p)
    (info,) = p.state.pending.values()
    assert info["filled_known"] == 5_000 and p.state.open == {}
    assert "incerto" in str(e.value) and p.risk.state.kill_switch and not p.armed
    assert len(ib.placed) == 3  # only the bracket: no flatten, nothing resent
    saved = p.store.get_state("pilot")
    assert saved is not None and saved["pending"]  # survives a restart for reconciliation


def test_replacement_is_a_free_candidate_not_one_already_active(tmp_path: Path) -> None:
    reg = registry(model("ridge-H1-a"), model("ridge-H1-b"), model("ridge-H1-c"), active=2)
    p, ib, _, clock = make(tmp_path, reg=reg, armed=True)
    p.state.returns["ridge-H1-a"] = [-1.0] * 4
    cycle(p)
    eid = next(e for e, t in p.state.open.items() if t["model_id"] == "ridge-H1-a")
    ib.hit(f"{eid}:sl")
    clock.now += timedelta(seconds=30)
    ib.tickers["EURUSD"].time = clock.now
    cycle(p)
    assert p.state.retired_ids == ["ridge-H1-a"]
    assert p.state.active_ids == ["ridge-H1-c", "ridge-H1-b"]  # the reserve takes the slot
