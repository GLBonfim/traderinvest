"""Simulator: hand-calculated accounting case, next-close fills, sizing and cash rules."""

from dataclasses import dataclass, field

import pandas as pd
import pytest

from app.trend.costs import CostModel
from app.trend.simulator import Context, simulate

D = pd.Timestamp
D0, D1, D2, D3, D4 = (
    D(x) for x in ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08")
)
RATE = pd.Series([0.036], index=pd.DatetimeIndex(["2023-12-29"]))  # 0.01 % per calendar day


@dataclass
class Script:
    """Scripted policy: fixed target weights on given decision days."""

    plan: dict[pd.Timestamp, dict[str, float]]
    assets: tuple[str, ...] = ("A", "B")
    seen: list[Context] = field(default_factory=list)

    def decide(self, ctx: Context) -> dict[str, float]:
        self.seen.append(ctx)
        return dict(self.plan.get(ctx.day, {}))


def _costs(**kw: float) -> CostModel:
    return CostModel(
        commission_rate=kw.get("rate", 0.001),
        commission_min=kw.get("minimum", 3.0),
        commission_max=None,
        half_spread={"A": 0.001, "B": 0.002},
        slippage={"A": 0.001, "B": 0.0},
    )


def _prices() -> dict[str, pd.Series]:
    a = pd.Series([10.0, 11.0, 12.0, 11.0, 12.0], index=pd.DatetimeIndex([D0, D1, D2, D3, D4]))
    b = pd.Series([20.0, 21.0, 22.0, 21.0], index=pd.DatetimeIndex([D0, D1, D3, D4]))  # no D2
    return {"A": a, "B": b}


def _master(prices: dict[str, pd.Series]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(sorted(set(prices["A"].index) | set(prices["B"].index)))


def test_hand_calculated_case() -> None:
    """Two assets, five days: entry, drift rebalance, exit on a holiday-shifted close, minimum
    commission, cash interest. Expected values computed by hand (see comments)."""
    prices = _prices()
    plan = {D0: {"A": 0.5, "B": 0.3}, D1: {"A": 0.6}, D2: {"B": 0.0}}
    result = simulate(prices, _master(prices), RATE, _costs(), Script(plan), 1000.0, D0, D4)
    nav = result.daily["nav"]
    # D0: decide A 50 units (0.5*1000/10), B 15 units (0.3*1000/20); nothing fills.
    assert nav[D0] == pytest.approx(1000.0)
    # D1: interest 1000*0.036/360 = 0.1; buy A 50 @11 (550, fee 3, spread 0.55, slip 0.55),
    # buy B 15 @21 (315, fee 3, spread 0.63): cash 127.37, holdings 865.
    assert result.daily.loc[D1, "cash"] == pytest.approx(127.37)
    assert nav[D1] == pytest.approx(992.37)
    # D1 decision: A to trunc(0.6*992.37/11) = 54 units -> buy 4.
    # D2: interest 0.012737; A marks 11->12 (+50); buy A 4 @12 (48, fee 3, 0.048, 0.048).
    assert nav[D2] == pytest.approx(1039.286737)
    # D2 decision: exit B; B has no D2 session, so it fills at its next close (D3) @22:
    # interest 0.0076286737; A 12->11 (-54); B 21->22 (+15); sale 330 - fee 3 - spread 0.66.
    assert result.daily.loc[D3, "cash"] == pytest.approx(402.6343656737)
    assert nav[D3] == pytest.approx(996.6343656737)
    # D4 (Monday, 3 calendar days): interest 402.6343656737*0.0003; A 11->12 (+54).
    assert nav[D4] == pytest.approx(1050.7551559834021)
    assert result.daily["interest"].sum() == pytest.approx(0.2411559834021)
    assert result.asset_pnl["A"] == pytest.approx(
        {"price": 50.0, "commission": 6.0, "spread": 0.598, "slippage": 0.598}
    )
    assert result.asset_pnl["B"] == pytest.approx(
        {"price": 15.0, "commission": 6.0, "spread": 1.29, "slippage": 0.0}
    )
    fills = [(t["date"], t["asset"], t["quantity"]) for t in result.trades]
    assert fills == [(D1, "A", 50), (D1, "B", 15), (D2, "A", 4), (D3, "B", -15)]


def test_identities_hold_every_day() -> None:
    prices = _prices()
    plan = {D0: {"A": 0.5, "B": 0.3}, D1: {"A": 0.6}, D2: {"B": 0.0}}
    r = simulate(prices, _master(prices), RATE, _costs(), Script(plan), 1000.0, D0, D4)
    d = r.daily
    assert (d["nav"] - d["cash"] - d["holdings"]).abs().max() < 1e-9
    change = d["nav"].diff().iloc[1:]
    explained = d["price_pnl"] + d["interest"] - d["commission"] - d["spread"] - d["slippage"]
    assert (change - explained.iloc[1:]).abs().max() < 1e-9


def test_order_waits_for_the_assets_next_close_and_is_not_duplicated() -> None:
    prices = _prices()
    prices["B"] = prices["B"].drop([D1])  # B trades on D0, D3, D4 only
    plan = {d: {"B": 0.3} for d in (D0, D1, D2)}  # policy keeps asking every day
    script = Script(plan, assets=("B",))
    r = simulate({"B": prices["B"]}, _master(prices), RATE, _costs(), script, 1000.0, D0, D4)
    assert [(t["date"], t["quantity"]) for t in r.trades] == [(D3, 15)]
    assert D(script.seen[1].day) == D1 and "B" in script.seen[1].pending
    assert "B" in script.seen[2].pending  # still pending at D(D2): no second order


def test_weekend_fill_and_decision_without_future_close() -> None:
    prices = _prices()
    plan = {D3: {"A": 0.5}, D4: {"B": 0.2}}  # Friday decision fills Monday; D4 has no future
    r = simulate(prices, _master(prices), RATE, _costs(), Script(plan), 1000.0, D0, D4)
    assert [(t["date"], t["asset"]) for t in r.trades] == [(D4, "A")]
    # D4 NAV 996.52012 -> B trunc(0.2 * 996.52012 / 21) = 9 units, never filled
    assert r.pending_at_end == [{"asset": "B", "quantity": 9, "decided": D4}]


def test_policy_sees_only_closes_up_to_the_decision_day() -> None:
    prices = _prices()
    script = Script({})
    simulate(prices, _master(prices), RATE, _costs(), script, 1000.0, D0, D4)
    seen = {ctx.day: ctx.prices for ctx in script.seen}
    assert seen[D2] == {"A": 12.0, "B": 21.0}  # B carries its D1 close on its holiday
    assert seen[D1] == {"A": 11.0, "B": 21.0}


def test_sells_fill_before_buys_and_cash_never_goes_negative() -> None:
    prices = {
        "A": pd.Series([10.0, 10.0, 12.0], index=pd.DatetimeIndex([D0, D1, D2])),
        "B": pd.Series([10.0, 10.0, 10.0], index=pd.DatetimeIndex([D0, D1, D2])),
    }
    master = pd.DatetimeIndex([D0, D1, D2])
    plan = {D0: {"B": 0.9}, D1: {"A": 0.9, "B": 0.0}}
    r = simulate(prices, master, RATE, _costs(), Script(plan), 100.0, D0, D2)
    day2 = [t for t in r.trades if t["date"] == D2]
    assert [t["asset"] for t in day2] == ["B", "A"]  # sell first, then buy
    # A asked trunc(0.9 * 96.83 / 10) = 8 units at D1; at 12, with 93.650683 EUR after the
    # sale, 8 * 12.024 + 3 > cash, 7 * 12.024 + 3 = 87.168 fits
    a = day2[1]
    assert a["requested"] == 8 and a["quantity"] == 7
    assert (r.daily["cash"] >= 0).all()


def test_zero_quantity_change_is_not_sent() -> None:
    prices = _prices()
    # D1 NAV 996.0; trunc(0.555 * 996 / 11) = 50 = units held -> no order
    plan = {D0: {"A": 0.5}, D1: {"A": 0.555}}
    r = simulate(prices, _master(prices), RATE, _costs(), Script(plan), 1000.0, D0, D4)
    assert [(t["date"], t["quantity"]) for t in r.trades] == [(D1, 50)]


def test_buy_that_cannot_afford_one_unit_is_dropped() -> None:
    prices = _prices()
    r = simulate(prices, _master(prices), RATE, _costs(), Script({D0: {"B": 1.0}}), 20.0, D0, D2)
    # 1 unit decided at 20; at 21 it costs 21.042 + 3 > 20.002 EUR of cash
    assert [(t["asset"], t["quantity"], t["status"]) for t in r.trades] == [
        ("B", 0, "dropped_no_cash")
    ]
    assert r.daily["holdings"].iloc[-1] == 0.0
