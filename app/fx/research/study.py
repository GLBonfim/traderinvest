"""Calibration and out-of-sample validation of the v2 agents on IBKR M15 bid/ask history.

`python -m app.fx.research.study calibrate` -> development period only: small parameter grids,
    robust selection (t-statistic of mean net R, smoothed over grid neighbours), then the
    validation period for the selected parameters; writes the PRE-REGISTRATION (selected
    parameters + decision rule + hash) BEFORE any holdout number exists.
`python -m app.fx.research.study holdout` -> evaluates the pre-registered parameters ONCE on
    the holdout period and applies the pre-registered rule.

Periods (bar entry time, UTC): development 2020-10-17..2024-07-01, validation 2024-07-01..
2025-10-01, holdout 2025-10-01..2026-10-17. Features are causal, so computing them over the
whole history leaks nothing; only trades are split by entry time.

Decision rule (pre-registered; an agent is APPROVED only if all hold on the holdout):
  1. at least 100 trades pooled over the nine pairs;
  2. mean net R > 0 with a one-sided moving-block bootstrap p-value, Holm-adjusted over the
     three directional agents, below 0.10;
  3. profit factor >= 1.05;
  4. mean net R > 0 on the validation period too;
  5. mean net R above the 90th percentile of a random-timing baseline (same signals' risk
     geometry, random bars and directions).
The regime filter is judged descriptively (with vs without its veto). Nothing is called
profitable without passing the rule.
"""

import argparse
import hashlib
import itertools
import json
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.agents.signals import (
    DEFAULT_REGIME,
    MeanReversionParams,
    MomentumParams,
    TrendParams,
    features,
    mean_reversion_signals,
    mid_frame,
    momentum_signals,
    regime_frame,
    trend_signals,
)
from app.fx.models import PAIRS, pair
from app.fx.research.backtest import DEFAULT_COSTS, CostModel, run_backtest, summarize
from app.fx.research.history import DATA_DIR, load_pair, resample, with_spread_floor
from app.validation.comparisons import holm
from app.validation.resampling import moving_block_indices

OUT_DIR = Path("data/fx/research")
PERIODS = {
    "development": ("2020-10-17", "2024-07-01"),
    "validation": ("2024-07-01", "2025-10-01"),
    "holdout": ("2025-10-01", "2026-10-17"),
}
MAX_SPREAD_PIPS = 3.0  # the risk manager's entry check
RULE = {
    "min_trades": 100,
    "alpha_holm": 0.10,
    "min_profit_factor": 1.05,
    "validation_mean_r_positive": True,
    "random_baseline_percentile": 90,
}
GRIDS: dict[str, dict[str, list[float]]] = {
    "trend": {
        "stop_atr": [1.5, 2.0, 2.5],
        "target_atr": [2.0, 3.0, 4.0],
        "min_slope_atr": [0.3, 0.6],
    },
    "momentum": {"channel": [20, 40], "stop_atr": [1.0, 1.5, 2.0], "target_atr": [2.0, 3.0]},
    "mean_reversion": {"band_k": [2.0, 2.5], "max_er": [0.2, 0.3], "stop_buffer_atr": [0.5, 1.0]},
}
PARAM_TYPES: dict[str, type] = {
    "trend": TrendParams,
    "momentum": MomentumParams,
    "mean_reversion": MeanReversionParams,
}
SIGNALS: dict[str, Callable[[pd.DataFrame, Any], pd.DataFrame]] = {
    "trend": trend_signals,
    "momentum": momentum_signals,
    "mean_reversion": mean_reversion_signals,
}


@dataclass
class PairData:
    symbol: str
    df: pd.DataFrame  # bid_/ask_ OHLC
    feat: pd.DataFrame
    regime: pd.DataFrame
    base_allowed: pd.Series  # tradable hours and spread within the risk limit


def load_all(
    root: Path = DATA_DIR, symbols: list[str] | None = None, timeframe: str = "M15"
) -> dict[str, PairData]:
    """All pairs with the conservative spread floor, at M15 or resampled to H1."""
    out = {}
    for sym in symbols or list(PAIRS):
        df = with_spread_floor(load_pair(root, sym), sym)
        if timeframe == "H1":
            df = resample(df, "1h")
        spread = df["ask_close"] - df["bid_close"]
        f = features(mid_frame(df), spread)
        reg = regime_frame(f, DEFAULT_REGIME)
        spread_ok = spread / pair(sym).pip_size <= MAX_SPREAD_PIPS
        out[sym] = PairData(sym, df, f, reg, f["tradable"] & spread_ok)
    return out


def combos(agent: str) -> Iterator[dict[str, float]]:
    grid = GRIDS[agent]
    keys = list(grid)
    for values in itertools.product(*(grid[k] for k in keys)):
        c = dict(zip(keys, values, strict=True))
        if "target_atr" in c and c["target_atr"] / c["stop_atr"] < 1.2:
            continue  # the risk manager refuses risk/reward below 1.2
        yield c


def make_params(agent: str, combo: dict[str, float]) -> Any:
    base = PARAM_TYPES[agent]()
    typed = {k: type(getattr(base, k))(v) for k, v in combo.items()}
    return replace(base, **typed)


def agent_trades(
    data: dict[str, PairData],
    agent: str,
    params: Any,
    use_regime: bool = True,
    costs: CostModel = DEFAULT_COSTS,
) -> pd.DataFrame:
    frames = []
    for pdata in data.values():
        sig = SIGNALS[agent](pdata.feat, params)
        allowed = pdata.base_allowed & (~pdata.regime["veto"] if use_regime else True)
        t = run_backtest(pdata.df, sig, pdata.symbol, allowed, costs)
        if len(t):
            t["regime"] = pdata.regime["regime"].reindex(t["signal_ts"]).to_numpy()
            frames.append(t)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("entry_ts", ignore_index=True)


def in_period(trades: pd.DataFrame, period: str) -> pd.DataFrame:
    if trades.empty:
        return trades
    lo, hi = (pd.Timestamp(x, tz="UTC") for x in PERIODS[period])
    return trades[(trades["entry_ts"] >= lo) & (trades["entry_ts"] < hi)]


TIMEFRAMES = ("M15", "H1")


def period_days(period: str) -> float:
    lo, hi = (pd.Timestamp(x) for x in PERIODS[period])
    return float((hi - lo).days)


def one_sided_bootstrap_p(r: np.ndarray, n_resamples: int = 4000, seed: int = 7) -> float:
    """H0: mean <= 0 vs H1: mean > 0, moving blocks over the time-ordered trades."""
    n = len(r)
    if n < 10:
        return float("nan")
    est = float(r.mean())
    if est <= 0:
        return 1.0
    block = max(1, round(n ** (1 / 3)))
    idx = moving_block_indices(n, block, n_resamples, seed)
    means = r[idx].mean(axis=1)
    return float((1 + np.count_nonzero(means - est >= est)) / (n_resamples + 1))


def random_baseline(
    data: dict[str, PairData],
    agent: str,
    params: Any,
    period: str,
    reps: int = 100,
    seed: int = 11,
    costs: CostModel = DEFAULT_COSTS,
) -> np.ndarray:
    """Mean net R of `reps` random-timing strategies: the agent's own signal rows (stop,
    target, hold) moved to random allowed bars of the period with random directions."""
    rng = np.random.default_rng(seed)
    lo, hi = (pd.Timestamp(x, tz="UTC") for x in PERIODS[period])
    means = np.zeros(reps)
    prepared = []
    for pdata in data.values():
        sig = SIGNALS[agent](pdata.feat, params)
        allowed = pdata.base_allowed & ~pdata.regime["veto"]
        mask = allowed & (sig.index >= lo) & (sig.index < hi)
        rows = sig[(sig["dir"] != 0) & mask]
        pool = np.flatnonzero(mask.to_numpy())
        prepared.append((pdata, sig, rows, pool))
    for k in range(reps):
        rs = []
        for pdata, sig, rows, pool in prepared:
            if len(rows) == 0 or len(pool) == 0:
                continue
            pos = rng.choice(pool, size=min(len(rows), len(pool)), replace=False)
            cols = {c: sig[c].to_numpy(copy=True) for c in sig.columns}
            cols["dir"] = np.zeros(len(sig), dtype=int)
            cols["dir"][pos] = rng.choice([-1, 1], size=len(pos))
            for col in ("stop_dist", "target_dist", "max_hold"):
                cols[col][pos] = rows[col].to_numpy()[: len(pos)]
            fake = pd.DataFrame(cols, index=sig.index)
            t = run_backtest(pdata.df, fake, pdata.symbol, None, costs)
            rs.append(t["r_net"].to_numpy())
        means[k] = np.concatenate(rs).mean() if rs and sum(len(x) for x in rs) else 0.0
    return means


def neighbour_smoothed(scores: dict[tuple[float, ...], float], keys: list[str]) -> dict[Any, float]:
    """0.5 x own score + 0.5 x mean score of grid neighbours (one parameter one step away)."""
    grid_values = {k: sorted({c[i] for c in scores}) for i, k in enumerate(keys)}
    out = {}
    for c, s in scores.items():
        neigh = []
        for i, k in enumerate(keys):
            vals = grid_values[k]
            pos = vals.index(c[i])
            for dp in (-1, 1):
                if 0 <= pos + dp < len(vals):
                    nb = list(c)
                    nb[i] = vals[pos + dp]
                    if tuple(nb) in scores:
                        neigh.append(scores[tuple(nb)])
        out[c] = 0.5 * s + 0.5 * (float(np.mean(neigh)) if neigh else s)
    return out


def breakdown(trades: pd.DataFrame, by: str) -> dict[str, dict[str, float]]:
    return {str(k): summarize(g) for k, g in trades.groupby(by)} if len(trades) else {}


def calibrate(data: dict[str, PairData]) -> dict[str, Any]:
    result: dict[str, Any] = {"periods": PERIODS, "rule": RULE, "agents": {}}
    for agent in GRIDS:
        keys = list(GRIDS[agent])
        table = []
        scores: dict[tuple[float, ...], float] = {}
        for combo in combos(agent):
            trades = in_period(agent_trades(data, agent, make_params(agent, combo)), "development")
            s = summarize(trades, period_days("development"))
            positive_pairs = (
                sum(1 for _, g in trades.groupby("pair") if g["r_net"].mean() > 0)
                if len(trades)
                else 0
            )
            score = s["t_stat"] if s["trades"] >= 200 else -99.0
            scores[tuple(combo[k] for k in keys)] = score
            table.append({"params": combo, "dev": s, "positive_pairs": positive_pairs})
            print(
                f"{agent} {combo} dev: n={s['trades']:.0f} meanR={s['mean_r']:+.3f} "
                f"t={s['t_stat']:+.2f} pairs+={positive_pairs}",
                flush=True,
            )
        smooth = neighbour_smoothed(scores, keys)
        best = max(smooth, key=lambda c: smooth[c])
        chosen = dict(zip(keys, best, strict=True))
        params = make_params(agent, chosen)
        trades_all = agent_trades(data, agent, params)
        val = in_period(trades_all, "validation")
        val_no_regime = in_period(agent_trades(data, agent, params, use_regime=False), "validation")
        result["agents"][agent] = {
            "grid": table,
            "dev_score": float(smooth[best]),
            "chosen": chosen,
            "params": asdict(params),
            "dev": summarize(in_period(trades_all, "development"), period_days("development")),
            "validation": summarize(val, period_days("validation")),
            "validation_without_regime_filter": summarize(val_no_regime),
            "validation_by_pair": breakdown(val, "pair"),
        }
        print(
            f"==> {agent} chosen {chosen} val: {result['agents'][agent]['validation']}", flush=True
        )
    return result


def calibrate_all(data_by_tf: dict[str, dict[str, PairData]]) -> dict[str, Any]:
    """Calibrates on each timeframe; the SYSTEM timeframe (one for all agents, as the product
    runs one) is the one with the best summed development score. Development data only."""
    by_tf = {}
    for tf, data in data_by_tf.items():
        print(f"===== timeframe {tf}", flush=True)
        by_tf[tf] = calibrate(data)
    totals = {tf: sum(a["dev_score"] for a in r["agents"].values()) for tf, r in by_tf.items()}
    chosen = max(totals, key=lambda tf: totals[tf])
    return {
        "timeframe": chosen,
        "dev_score_by_timeframe": totals,
        "by_timeframe": by_tf,
        "periods": PERIODS,
        "rule": RULE,
    }


def preregister(full: dict[str, Any]) -> dict[str, Any]:
    tf = full["timeframe"]
    result = full["by_timeframe"][tf]
    reg = {
        "created_at": datetime.now(UTC).isoformat(),
        "timeframe": tf,
        "holdout": PERIODS["holdout"],
        "rule": RULE,
        "agents": {
            a: {"params": v["params"], "validation_mean_r": v["validation"]["mean_r"]}
            for a, v in result["agents"].items()
        },
        "costs": asdict(DEFAULT_COSTS),
    }
    body = json.dumps(reg, sort_keys=True, default=str)
    reg["sha256"] = hashlib.sha256(body.encode()).hexdigest()
    return reg


def holdout(data: dict[str, PairData], reg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"preregistration_sha256": reg["sha256"], "agents": {}}
    pvals = []
    names = list(reg["agents"])
    for agent in names:
        params = PARAM_TYPES[agent](**reg["agents"][agent]["params"])
        trades = in_period(agent_trades(data, agent, params), "holdout")
        r = trades["r_net"].to_numpy(float) if len(trades) else np.array([])
        s = summarize(trades, period_days("holdout"))
        p = one_sided_bootstrap_p(r)
        base = random_baseline(data, agent, params, "holdout")
        no_reg = summarize(
            in_period(agent_trades(data, agent, params, use_regime=False), "holdout")
        )
        costs2 = replace(
            DEFAULT_COSTS, commission_bps=0.4, financing_bps_per_night=1.0, extra_slippage_pips=0.2
        )
        stressed = summarize(in_period(agent_trades(data, agent, params, costs=costs2), "holdout"))
        out["agents"][agent] = {
            "holdout": s,
            "p_one_sided": p,
            "random_baseline_mean_r_p90": float(np.percentile(base, 90)),
            "random_baseline_mean_r_median": float(np.median(base)),
            "holdout_without_regime_filter": no_reg,
            "holdout_stressed_costs": stressed,
            "by_pair": breakdown(trades, "pair"),
            "by_regime": breakdown(trades, "regime"),
            "by_exit": breakdown(trades, "reason"),
            "equity_r": trades["r_net"].cumsum().round(3).tolist() if len(trades) else [],
        }
        pvals.append(p)
    adj = holm(np.array(pvals, dtype=float))
    for agent, padj in zip(names, adj, strict=True):
        a = out["agents"][agent]
        h = a["holdout"]
        checks = {
            "min_trades": h["trades"] >= RULE["min_trades"],
            "holm_p": bool(np.isfinite(padj) and padj < RULE["alpha_holm"]),
            "profit_factor": h["profit_factor"] >= RULE["min_profit_factor"],
            "validation_positive": reg["agents"][agent]["validation_mean_r"] > 0,
            "beats_random_p90": h["mean_r"] > a["random_baseline_mean_r_p90"],
        }
        a["p_holm"] = float(padj)
        a["checks"] = checks
        a["approved"] = all(checks.values())
    return out


def system(data: dict[str, PairData], reg: dict[str, Any], ho: dict[str, Any]) -> dict[str, Any]:
    """The whole product (orchestrator, coordinator, risk, venue) replayed on the holdout:
    once with the APPROVED agents only (what would trade), once with all three agents as a
    diagnosis of the portfolio rules (labelled as such)."""
    from app.fx.proposals import AgentRole
    from app.fx.research.replay import run_replay

    tf = reg["timeframe"]
    lo, hi = (pd.Timestamp(x, tz="UTC").to_pydatetime() for x in PERIODS["holdout"])
    roles = {
        "trend": AgentRole.TREND,
        "momentum": AgentRole.MOMENTUM,
        "mean_reversion": AgentRole.MEAN_REVERSION,
    }
    signals, holds = {}, {}
    for name, role in roles.items():
        params = PARAM_TYPES[name](**reg["agents"][name]["params"])
        signals[role] = {s: SIGNALS[name](d.feat, params) for s, d in data.items()}
        holds[role] = int(params.max_hold)
    frames = {s: d.df for s, d in data.items()}
    feats = {s: d.feat for s, d in data.items()}
    regimes = {s: d.regime for s, d in data.items()}
    approved = frozenset(a for a, v in ho["agents"].items() if v["approved"])
    out = {"timeframe": tf, "approved": sorted(approved)}
    out["approved_only"] = run_replay(
        frames, lo, hi, signals, holds, feats, regimes, approved, timeframe=tf
    )
    out["diagnostic_all_agents"] = run_replay(
        frames, lo, hi, signals, holds, feats, regimes, frozenset(roles), timeframe=tf
    )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="traderinvest-fx-study")
    ap.add_argument("step", choices=["calibrate", "holdout", "system"])
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    a = ap.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data_by_tf = {tf: load_all(a.data, timeframe=tf) for tf in TIMEFRAMES}
    reg_path = OUT_DIR / "preregistration.json"
    if a.step == "calibrate":
        result = calibrate_all(data_by_tf)
        (OUT_DIR / "calibration.json").write_text(json.dumps(result, indent=1, default=str))
        reg = preregister(result)
        reg_path.write_text(json.dumps(reg, indent=1, default=str))
        print(f"pre-registered: {reg['sha256']}")
    elif a.step == "system":
        reg = json.loads(reg_path.read_text())
        ho = json.loads((OUT_DIR / "holdout.json").read_text())
        res = system(data_by_tf[reg["timeframe"]], reg, ho)
        (OUT_DIR / "system.json").write_text(json.dumps(res, indent=1, default=str))
        for k in ("approved_only", "diagnostic_all_agents"):
            r = res[k]
            print(
                k,
                {
                    x: r[x]
                    for x in (
                        "return_pct",
                        "max_drawdown_pct",
                        "entries_filled",
                        "max_entries_one_day",
                        "journal_chain_ok",
                    )
                },
            )
    else:
        reg = json.loads(reg_path.read_text())
        if (OUT_DIR / "holdout.json").exists():
            raise SystemExit("the holdout was already evaluated once; it is not re-run")
        res = holdout(data_by_tf[reg["timeframe"]], reg)
        (OUT_DIR / "holdout.json").write_text(json.dumps(res, indent=1, default=str))
        for agent, v in res["agents"].items():
            print(agent, "APPROVED" if v["approved"] else "NOT APPROVED", v["checks"], v["holdout"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
