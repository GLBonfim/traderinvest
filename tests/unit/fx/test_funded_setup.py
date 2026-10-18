"""One-time paper quote-currency buffers: gates, write-ahead, idempotency, verification."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.fx.ibkr.funded_setup import (
    FUND_PAIRS,
    FundingError,
    coid_for,
    ensure_funding,
    funding_status,
)
from tests.unit.fx.ibkr_fakes import NOW, FakeIB, executor, ticker

QUOTES = {"EURUSD": (1.1000, 1.10006), "EURGBP": (0.8600, 0.86006), "EURJPY": (160.00, 160.01)}


def session(now: datetime = NOW) -> FakeIB:
    ib = FakeIB()
    for sym, (bid, ask) in QUOTES.items():
        ib.tickers[sym] = ticker(sym, bid, ask, ts=now)
    return ib


def fund(ib: FakeIB, root: Path, now: datetime = NOW, port: int = 4002) -> dict[str, Any]:
    return asyncio.run(
        ensure_funding(ib, root, executor=executor(ib, lambda: now), port=port, clock=lambda: now)
    )


def test_complete_once_with_three_conversions_and_baseline(tmp_path: Path) -> None:
    ib = session()
    out = fund(ib, tmp_path)
    assert out["state"] == "complete"
    assert out["baseline_positions"] == {s: -1000.0 for s in FUND_PAIRS}
    orders = [o for _, o in ib.placed]
    assert [o.orderRef for o in orders] == [coid_for(s) for s in FUND_PAIRS]
    assert {(o.orderType, o.action, o.totalQuantity, o.tif) for o in orders} == {
        ("MKT", "SELL", 1000.0, "DAY")
    }
    assert {o.account for o in orders} == {"DU1234567"}
    assert out["quote_cash_eur"]["USD"] == pytest.approx(1000, rel=0.01)
    again = fund(ib, tmp_path)  # idempotent: nothing is sent twice
    assert again["state"] == "complete" and len(ib.placed) == 3
    assert funding_status(tmp_path)["state"] == "complete"  # type: ignore[index]


def test_own_journal_separate_from_agent_trades(tmp_path: Path) -> None:
    from app.fx.store import FxStore

    fund(session(), tmp_path)
    store = FxStore(tmp_path / "funding.sqlite")
    try:
        kinds = [e["kind"] for e in store.recent(100)]
        assert store.verify()[0]
    finally:
        store.close()
    assert kinds.count("funding_order_sent") == 3 and "funding_complete" in kinds
    assert not (tmp_path / "pilot.sqlite").exists()


def test_intention_persisted_before_each_submit(tmp_path: Path) -> None:
    ib = session()
    seen: list[str] = []

    def check(order: Any) -> None:
        saved = json.loads((tmp_path / "funding.json").read_text(encoding="utf-8"))
        sym = order.orderRef.split("-")[2]
        assert saved["orders"][sym]["status"] == "submitting"
        seen.append(sym)

    ib.on_place = check
    fund(ib, tmp_path)
    assert seen == list(FUND_PAIRS)


def test_market_closed_sends_nothing(tmp_path: Path) -> None:
    sat = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    ib = session(sat)
    assert fund(ib, tmp_path, now=sat)["state"] == "waiting_market_open"
    assert ib.placed == []


def test_rollover_window_sends_nothing(tmp_path: Path) -> None:
    near = datetime(2026, 10, 21, 20, 50, tzinfo=UTC)  # 16:50 New York
    ib = session(near)
    assert fund(ib, tmp_path, now=near)["state"] == "waiting_market_open"
    assert ib.placed == []


@pytest.mark.parametrize("bad", ["stale", "frozen", "wide", "missing"])
def test_no_live_fresh_quotes_sends_nothing(tmp_path: Path, bad: str) -> None:
    ib = session()
    t = ib.tickers["EURJPY"]
    if bad == "stale":
        t.time = NOW - timedelta(minutes=2)
    elif bad == "frozen":
        t.marketDataType = 2
    elif bad == "wide":
        t.ask = t.bid + 0.05  # 5 pips
    else:
        del ib.tickers["EURJPY"]
    assert fund(ib, tmp_path)["state"] == "waiting_quotes"
    assert ib.placed == []


def test_live_account_pauses_and_stays_paused(tmp_path: Path) -> None:
    ib = session()
    ib.accounts = ["DU1234567", "U7654321"]
    with pytest.raises(FundingError):
        fund(ib, tmp_path)
    ib.accounts = ["DU1234567"]
    with pytest.raises(FundingError, match="pausada"):
        fund(ib, tmp_path)  # a paused setup needs the owner's review
    assert ib.placed == []


def test_live_port_refused(tmp_path: Path) -> None:
    ib = session()
    with pytest.raises(FundingError, match="porta"):
        fund(ib, tmp_path, port=4001)
    assert ib.placed == []


def test_low_eur_cash_pauses(tmp_path: Path) -> None:
    ib = session()
    ib.cash = {"EUR": 9_999.0}
    with pytest.raises(FundingError, match="EUR"):
        fund(ib, tmp_path)
    assert ib.placed == []


def test_prior_positions_in_pairs_block_setup(tmp_path: Path) -> None:
    ib = session()
    ib.positions["EURGBP"] = -2_500.0  # not ours: never absorbed into the baseline
    with pytest.raises(FundingError, match="posições prévias"):
        fund(ib, tmp_path)
    assert ib.placed == []


def test_ambiguous_order_is_reconciled_never_resent(tmp_path: Path) -> None:
    ib = session()
    ib.fill_market = False
    assert fund(ib, tmp_path)["state"] == "waiting_positions"
    assert len(ib.placed) == 1
    for _ in range(2):
        assert fund(ib, tmp_path)["state"] == "waiting_positions"
    with pytest.raises(FundingError, match="sem confirmação"):
        fund(ib, tmp_path)
    assert len(ib.placed) == 1


def test_late_fill_is_adopted_then_setup_continues(tmp_path: Path) -> None:
    ib = session()
    ib.fill_market = False
    fund(ib, tmp_path)
    first = ib.by_ref(coid_for("EURUSD"))
    ib._fill(first, 1.1000)
    ib.fill_market = True
    out = fund(ib, tmp_path)
    assert out["state"] == "complete" and len(ib.placed) == 3


def test_crash_after_write_ahead_never_resends(tmp_path: Path) -> None:
    ib = session()
    (tmp_path / "funding.json").write_text(
        json.dumps(
            {
                "schema": "fx-paper-funding/1",
                "state": "waiting_positions",
                "verify_calls": 0,
                "orders": {
                    "EURUSD": {
                        "coid": coid_for("EURUSD"),
                        "side": "SELL",
                        "units": 1000.0,
                        "status": "submitting",
                        "lookups": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    for _ in range(2):
        assert fund(ib, tmp_path)["state"] == "waiting_positions"
    with pytest.raises(FundingError):
        fund(ib, tmp_path)
    assert ib.placed == []


def test_read_only_gateway_pauses(tmp_path: Path) -> None:
    ib = session()
    ib.read_only = True
    with pytest.raises(FundingError, match="Read-Only"):
        fund(ib, tmp_path)
    assert len(ib.placed) == 1 and funding_status(tmp_path)["state"] == "paused"  # type: ignore[index]


def test_partial_conversion_pauses(tmp_path: Path) -> None:
    ib = session()
    ib.partial = 400.0
    with pytest.raises(FundingError, match="parcial"):
        fund(ib, tmp_path)
    assert len(ib.placed) == 1


def test_fills_not_visible_keep_strategies_disarmed(tmp_path: Path) -> None:
    ib = session()
    ib.report_positions = False  # IBKR shows conversions as cash only
    for _ in range(9):
        out = fund(ib, tmp_path)
        assert out["state"] == "waiting_positions" and "baseline_positions" not in out
    with pytest.raises(FundingError, match="DESARMADAS"):
        fund(ib, tmp_path)
    assert len(ib.placed) == 3


def test_quote_cash_below_half_buffer_is_not_complete(tmp_path: Path) -> None:
    ib = session()
    fund_out = None
    real_fill = ib._fill

    def leaky(trade: Any, price: float, qty: float | None = None) -> None:
        real_fill(trade, price, qty)
        ib.cash["JPY"] = 10_000.0  # ~62 EUR: the buffer did not arrive

    ib._fill = leaky  # type: ignore[method-assign]
    fund_out = fund(ib, tmp_path)
    assert fund_out["state"] == "waiting_positions"
    assert any("JPY" in p for p in fund_out["problems"])


def test_stop_file_before_start_sends_nothing_and_resumes(tmp_path: Path) -> None:
    ib = session()
    (tmp_path / "STOP").write_text("")
    assert fund(ib, tmp_path)["state"] == "stopped" and ib.placed == []
    (tmp_path / "STOP").unlink()
    assert fund(ib, tmp_path)["state"] == "complete" and len(ib.placed) == 3


def test_stop_created_during_awaits_vetoes_the_send(tmp_path: Path) -> None:
    ib = session()

    def stop_now() -> None:
        (tmp_path / "STOP").write_text("")  # while qualifying the contract

    ib.on_qualify = stop_now
    out = fund(ib, tmp_path)
    assert out["state"] == "stopped" and ib.placed == []
    assert out["orders"] == {}  # the vetoed intention was withdrawn: nothing ambiguous
    ib.on_qualify = None
    (tmp_path / "STOP").unlink()
    assert fund(ib, tmp_path)["state"] == "complete" and len(ib.placed) == 3


def test_stop_between_conversions_keeps_the_filled_one(tmp_path: Path) -> None:
    ib = session()

    def stop_after_send(order: Any) -> None:
        (tmp_path / "STOP").write_text("")  # after the first send

    ib.on_place = stop_after_send
    out = fund(ib, tmp_path)
    assert out["state"] == "stopped" and len(ib.placed) == 1
    assert out["orders"]["EURUSD"]["status"] == "filled" and "EURGBP" not in out["orders"]
    ib.on_place = None
    (tmp_path / "STOP").unlink()
    assert fund(ib, tmp_path)["state"] == "complete" and len(ib.placed) == 3


@pytest.mark.parametrize(
    "accounts", [["DU1234567", "DU7654321"], ["DU1234567", "U7654321"], ["U7654321"], []]
)
def test_exactly_one_paper_account_required(tmp_path: Path, accounts: list[str]) -> None:
    ib = session()
    ib.accounts = accounts
    with pytest.raises(FundingError):
        fund(ib, tmp_path)
    assert ib.placed == [] and funding_status(tmp_path)["state"] == "paused"  # type: ignore[index]


def test_second_account_appearing_before_send_vetoes_it(tmp_path: Path) -> None:
    ib = session()

    def add_account() -> None:
        ib.accounts = ["DU1234567", "DU7654321"]

    ib.on_qualify = add_account
    with pytest.raises(FundingError, match="exatamente UMA"):
        fund(ib, tmp_path)
    saved = funding_status(tmp_path)
    assert ib.placed == [] and saved is not None and saved["orders"] == {}
