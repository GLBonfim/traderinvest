"""Causality and exact reproduction of the full strategy replay (design v2 section 13)."""

from typing import Any

import numpy as np
import pandas as pd

from app.trend.costs import CostModel
from app.trend.runner import record_sha256, result_record
from app.trend.simulator import RunResult, simulate
from app.trend.strategy import TrendPolicy, signal_table

RATE = pd.Series([0.02], index=pd.DatetimeIndex(["2019-12-31"]))
COSTS = CostModel(
    commission_rate=0.0005,
    commission_min=3.0,
    commission_max=None,
    half_spread=dict.fromkeys("ABC", 0.0005),
    slippage=dict.fromkeys("ABC", 0.0003),
)


def _prices(seed: int = 7, n: int = 700) -> dict[str, pd.Series]:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2020-01-01", periods=n)
    out: dict[str, pd.Series] = {}
    for k, name in enumerate("ABC"):
        regime = np.repeat(rng.choice([-0.002, 0.002], size=n // 100 + 1), 100)[:n]
        values = 50 * np.cumprod(1 + regime + rng.normal(0, 0.012, n))
        idx = days if name == "A" else days[(np.arange(n) + k) % 17 != 0]  # own holidays
        out[name] = pd.Series(values[: len(idx)], index=idx)
    return out


def _run(prices: dict[str, pd.Series], end: pd.Timestamp) -> tuple[RunResult, TrendPolicy]:
    tables = {a: signal_table(p) for a, p in prices.items()}
    policy = TrendPolicy(tables, tuple(sorted(prices)))
    master = pd.DatetimeIndex(sorted(set().union(*(set(p.index) for p in prices.values()))))
    start = master[260]
    return simulate(prices, master, RATE, COSTS, policy, 10_000.0, start, end), policy


def _upto(result: RunResult, policy: TrendPolicy, t: pd.Timestamp) -> dict[str, Any]:
    return {
        "daily": result.daily.loc[result.daily.index <= t].to_dict("index"),
        "fills": [x for x in result.trades if x["date"] <= t],
        "orders": sorted(
            (x["decided"], x["asset"], x["requested"]) for x in result.trades if x["decided"] <= t
        )
        + sorted(
            (p["decided"], p["asset"], p["quantity"])
            for p in result.pending_at_end
            if p["decided"] <= t
        ),
        "decisions": [d for d in policy.decisions if pd.Timestamp(str(d["date"])) <= t],
    }


def test_future_data_never_changes_the_past() -> None:
    prices = _prices()
    full_end = max(p.index[-1] for p in prices.values())
    base, base_policy = _run(prices, full_end)
    assert sum(1 for t in base.trades if t["status"] == "filled") > 10  # the case trades
    master = base.daily.index
    rng = np.random.default_rng(3)
    for t in (master[40], master[200], master[350]):
        reference = _upto(base, base_policy, t)
        mutated = {
            a: p.where(p.index <= t, p * rng.uniform(0.5, 1.5, len(p))) for a, p in prices.items()
        }
        deleted = {
            a: p.loc[(p.index <= t) | (np.arange(len(p)) % 3 != 0)] for a, p in prices.items()
        }
        extra_days = pd.bdate_range(full_end + pd.Timedelta(days=1), periods=30)
        appended = {
            a: pd.concat([p, pd.Series(np.full(30, 1.0), index=extra_days)])
            for a, p in prices.items()
        }
        truncated = {a: p.loc[p.index <= t] for a, p in prices.items()}
        for variant in (mutated, deleted, appended, truncated):
            end = max(p.index[-1] for p in variant.values())
            result, policy = _run(variant, end)
            got = _upto(result, policy, t)
            assert got["daily"] == reference["daily"]
            assert got["fills"] == reference["fills"]
            assert got["decisions"] == reference["decisions"]
            decided_before = [o for o in reference["orders"] if o[0] < t]
            assert [o for o in got["orders"] if o[0] < t] == decided_before
            assert [o for o in got["orders"] if o[0] == t] == [
                o for o in reference["orders"] if o[0] == t
            ]


def test_two_runs_are_byte_identical() -> None:
    prices = _prices()
    end = max(p.index[-1] for p in prices.values())
    first = record_sha256(result_record(_run(prices, end)[0]))
    second = record_sha256(result_record(_run(_prices(), end)[0]))
    assert first == second
    other = record_sha256(result_record(_run(_prices(seed=8), end)[0]))
    assert other != first
