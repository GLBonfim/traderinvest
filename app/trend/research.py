"""Phase T3: development and validation replays (sealed test closed).

For each of the two open partitions, every cost scenario and every capital of the grid, the
strategy and the three benchmarks are replayed from cash. The development partition is loaded
only up to its own end, so development runs never see validation rows.
"""

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.trend.costs import SCENARIOS, cost_model
from app.trend.data import Market, build_market
from app.trend.metrics import (
    BLOCK_LENGTH,
    N_RESAMPLES,
    SEED,
    gates,
    sharpe_uncertainty,
    summarise,
)
from app.trend.runner import make_policy, record_sha256, result_record
from app.trend.signal import CASH
from app.trend.simulator import RunResult, simulate
from app.trend.strategy import TrendPolicy
from app.trend.universe import Universe
from app.validation.resampling import bootstrap_summary, moving_block_indices

CAPITALS = (500.0, 2_000.0, 5_000.0, 10_000.0, 25_000.0)
OPEN_PARTITIONS = ("development", "validation")
POLICY_NAMES = ("strategy", "equal_weight", "sixty_forty", "eur_cash")
VIABLE_COST_PCT = 0.50


def sixty_forty_legs(universe: Universe) -> dict[str, float]:
    legs = universe.raw["benchmarks"]["sixty_forty"]["legs"]
    return {leg.get("same_as_asset", leg.get("id")): float(leg["weight"]) for leg in legs}


def holdings_values(result: RunResult, prices: dict[str, pd.Series]) -> pd.DataFrame:
    """EUR value of each position at every master day of the run (rebuilt from the fills)."""
    days = pd.DatetimeIndex(result.daily.index)
    fills = [t for t in result.trades if t["status"] == "filled"]
    out = {}
    for asset, series in prices.items():
        qty = pd.Series(0.0, index=days)
        for t in fills:
            if t["asset"] == asset:
                qty.loc[qty.index >= t["date"]] += t["quantity"]
        px = series.reindex(days.union(pd.DatetimeIndex(series.index))).ffill().reindex(days)
        out[asset] = qty * px
    return pd.DataFrame(out, index=days).fillna(0.0)


def ter_drag_pct(values: pd.DataFrame, ter_pct: dict[str, float], daily: pd.DataFrame) -> float:
    """Annual TER cost inside the prices, as % of average NAV (informative)."""
    days = pd.DatetimeIndex(daily.index)
    if len(days) < 2 or values.empty:
        return 0.0
    gaps = np.diff(days.values).astype("timedelta64[D]").astype("float64")
    charge = sum(
        float((values[a].to_numpy()[:-1] * (ter_pct[a] / 100.0) * gaps / 365.0).sum())
        for a in values.columns
    )
    years = (days[-1] - days[0]).days / 365.25
    return 100.0 * charge / float(daily["nav"].mean()) / years


def drawdown_samples(returns: np.ndarray, idx: np.ndarray) -> np.ndarray:
    paths = np.cumprod(1.0 + returns[idx], axis=1)
    peaks = np.maximum.accumulate(paths, axis=1)
    return np.asarray((1.0 - paths / peaks).max(axis=1), dtype="float64")


def explain_trades(result: RunResult, policy: TrendPolicy) -> list[dict[str, Any]]:
    """One row per strategy order: why it was decided and how it was filled."""
    decisions = {(d["date"], d["asset"]): d for d in policy.decisions}
    rows = []
    for t in result.trades:
        d = decisions.get((t["decided"], t["asset"]), {})
        if d.get("state") == CASH:
            reason = "exit: state CASH"
        elif not d.get("w_actual"):
            reason = "entry: state LONG"
        else:
            reason = "drift: increase" if t["requested"] > 0 else "drift: reduce"
        rows.append(
            {
                "decided": t["decided"].date().isoformat(),
                "filled": t["date"].date().isoformat(),
                "asset": t["asset"],
                "reason": reason,
                "state": d.get("state"),
                "vol_60": d.get("vol"),
                "w_target": d.get("w_target"),
                "w_actual": d.get("w_actual"),
                "requested": t["requested"],
                "quantity": t["quantity"],
                "price_eur": t.get("price"),
                "value_eur": t.get("value"),
                "commission_eur": t.get("commission"),
                "spread_eur": t.get("spread"),
                "slippage_eur": t.get("slippage"),
                "status": t["status"],
            }
        )
    return rows


def run_partition(
    market: Market,
    universe: Universe,
    name: str,
    start: str,
    end: str,
) -> dict[str, Any]:
    tiers = {a.id: a.cost_class for a in universe.all_series()}
    ter = {a.id: a.product.ter_pct for a in universe.all_series()}
    legs = sixty_forty_legs(universe)
    out: dict[str, Any] = {"partition": name, "start": start, "end": end, "runs": {}}
    trades: dict[str, list[dict[str, Any]]] = {}
    for scenario in SCENARIOS:
        costs = cost_model(universe.raw, tiers, scenario)
        for capital in CAPITALS:
            for policy_name in POLICY_NAMES:
                policy = make_policy(policy_name, market, legs)
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
                    if scenario == "realistic":
                        trades[str(int(capital))] = explain_trades(result, policy)
                summary = summarise(result, market.cash_rate, capital)
                summary["ter_drag_pct"] = ter_drag_pct(
                    holdings_values(result, prices), ter, result.daily
                )
                summary["record_sha256"] = record_sha256(result_record(result))
                key = f"{policy_name}|{scenario}|{int(capital)}"
                out["runs"][key] = summary
    out["uncertainty"] = {}
    out["gates"] = {}
    for capital in CAPITALS:
        c = int(capital)
        strat = out["runs"][f"strategy|realistic|{c}"]
        ew = out["runs"][f"equal_weight|realistic|{c}"]
        spy = strat["sessions_per_year"]
        unc = sharpe_uncertainty(strat["_excess"], spy, benchmark_excess=ew["_excess"])
        idx = moving_block_indices(strat["_returns"].size, BLOCK_LENGTH, N_RESAMPLES, SEED)
        dd_diff = drawdown_samples(strat["_returns"], idx) - drawdown_samples(ew["_returns"], idx)
        unc["drawdown_difference_vs_equal_weight_95"] = bootstrap_summary(dd_diff, 0.95).__dict__
        out["uncertainty"][str(c)] = unc
        g = gates(strat, out["runs"][f"strategy|double|{c}"], ew)
        out["gates"][str(c)] = [g_.__dict__ for g_ in g]
    out["trades"] = trades
    return out


def minimum_viable_capital(partitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Smallest capital whose pooled dev+validation annual trading cost (strategy, realistic)
    is below 0.50 % of capital: total costs / sum over partitions of average NAV x years."""
    table = {}
    for capital in CAPITALS:
        key = f"strategy|realistic|{int(capital)}"
        cost = sum(sum(p["runs"][key]["costs"].values()) for p in partitions)
        nav_years = sum(
            float(p["runs"][key]["average_nav"]) * p["runs"][key]["years"] for p in partitions
        )
        table[str(int(capital))] = 100.0 * cost / nav_years
    viable = [int(c) for c in CAPITALS if table[str(int(c))] < VIABLE_COST_PCT]
    return {"annual_trading_cost_pct": table, "reference_capital": viable[0] if viable else None}


def run_t3(manifest: dict[str, Any], universe: Universe, data_root: Path) -> dict[str, Any]:
    parts = manifest["partitions"]
    results = []
    for name in OPEN_PARTITIONS:
        bounds = parts[name]
        market = build_market(manifest, universe, data_root, through=bounds["end"])
        results.append(run_partition(market, universe, name, bounds["start"], bounds["end"]))
    return {"partitions": results, "minimum_viable_capital": minimum_viable_capital(results)}


def json_ready(obj: Any) -> Any:
    """Drop private arrays (keys starting with '_') and make floats JSON-safe."""
    if isinstance(obj, dict):
        return {k: json_ready(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list | tuple):
        return [json_ready(v) for v in obj]
    if isinstance(obj, float | np.floating):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj
