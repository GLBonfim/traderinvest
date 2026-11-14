"""One replay = (market, partition window, scenario, capital, policy) -> RunResult.

Used by the T3/T4 commands; tests drive it with synthetic markets. Nothing here reads files.
"""

import hashlib
import json
from typing import Any

import pandas as pd

from app.trend.benchmarks import CashPolicy, equal_weight, sixty_forty
from app.trend.costs import CostModel
from app.trend.data import Market
from app.trend.simulator import Policy, RunResult, simulate
from app.trend.strategy import TrendPolicy, signal_table

POLICIES = ("strategy", "equal_weight", "sixty_forty", "eur_cash")


def make_policy(name: str, market: Market, sixty_forty_legs: dict[str, float]) -> Policy:
    if name == "strategy":
        tables = {
            a: signal_table(market.assets[a].prices["realistic"]) for a in market.universe_ids
        }
        return TrendPolicy(tables, market.universe_ids)
    if name == "equal_weight":
        return equal_weight(market.universe_ids, market.scheduled)
    if name == "sixty_forty":
        return sixty_forty(sixty_forty_legs, market.scheduled)
    if name == "eur_cash":
        return CashPolicy()
    raise ValueError(f"unknown policy {name!r}")


def run(
    market: Market,
    policy_name: str,
    scenario: str,
    costs: CostModel,
    capital: float,
    start: str,
    end: str,
    sixty_forty_legs: dict[str, float],
) -> RunResult:
    """Signals always come from the realistic series; the scenario sets prices and costs."""
    policy = make_policy(policy_name, market, sixty_forty_legs)
    prices = {a: market.assets[a].prices[scenario] for a in policy.assets}
    result = simulate(
        prices,
        market.master,
        market.cash_rate,
        costs,
        policy,
        capital,
        pd.Timestamp(start),
        pd.Timestamp(end),
    )
    if isinstance(policy, TrendPolicy):
        result.flags.extend(policy.flags)
    return result


def result_record(result: RunResult) -> dict[str, Any]:
    """Deterministic, JSON-ready view of a run (used for reproduction checks)."""

    def plain(value: Any) -> Any:
        if isinstance(value, pd.Timestamp):
            return value.date().isoformat()
        if isinstance(value, float):
            return float(f"{value:.12g}")
        return value

    daily = result.daily.reset_index()
    return {
        "daily": [{k: plain(v) for k, v in row.items()} for row in daily.to_dict("records")],
        "trades": [{k: plain(v) for k, v in t.items()} for t in result.trades],
        "asset_pnl": {a: {k: plain(v) for k, v in p.items()} for a, p in result.asset_pnl.items()},
        "flags": result.flags,
        "pending_at_end": [{k: plain(v) for k, v in p.items()} for p in result.pending_at_end],
    }


def record_sha256(record: dict[str, Any]) -> str:
    text = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()
