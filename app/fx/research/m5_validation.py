"""`python -m app.fx.research.m5_validation --data data/fx/ibkr-m5 --out <run dir>`

Walk-forward validation of the M5 candle-lab PIPELINE (not of a bare predictor):

    market analyst (closed M5 bars) -> specialist (real candle-pattern family) ->
    trained auxiliary model must AGREE -> SELL only (EUR-funded account) -> lab limits

Three specialists, two pre-declared candidates each (6 in total, no search beyond that):
reversao (reversal patterns, Ridge), continuacao (continuation, shallow ExtraTrees),
impulso (momentum, Logistic). Folds are expanding: train 2023 -> validate 2024; train
2023-2024 -> validate 2025. Labels are purged (horizon + 1 bars) with a 24 h embargo.

The 2026 period is a TEMPORAL M5 holdout: it is loaded and evaluated only after the
per-specialist selection is frozen in `selection.json`, and only for candidates that passed
the gate. It is not absolute statistical independence (2026 was seen by earlier M15
research). The protocol (every constant below) is written to `protocol.json` with its
SHA-256 BEFORE any market data is read. Nothing here approves real-money trading; a
specialist that fails is NO TRADE and no loser is promoted.

Costs follow the paper lab:
- risk 0.1 % of 10 000 EUR, capped at 5 000 EUR per trade (whole 1 000 units);
- commission max(2 USD, 0.2 bps) per side, financing 0.5 bps per night;
- bid/ask fills with the spread floor;
- a stress scenario adds 0.2 pip slippage per fill and multiplies costs by 1.5.

R is measured against the ACTUAL risk of the capped size.

Amendment 1 (ex ante, before ANY training or result; arithmetic of the 2 USD minimum per
order and the trade geometry only):
- the original geometry (2 ATR stop, 3 ATR target, 12-bar hold) was not viable: with the
  5 000 EUR cap and a ~6-pip M5 stop, the minimum commission alone cost ~1.33 R per trade;
- entries stay on M5, but the label horizon and the time stop are 96 bars (8 h), with the
  purge kept coherent at horizon + 1;
- stop distance = max(2 ATR, 2 x min commission (in the quote currency) / 0.45 / 5 000),
  i.e. the round-trip minimum fee is at most 0.45 R at the full cap; target = 2 x stop;
- after rounding units down to whole 1 000s (never forced up, never above the risk
  budget), a trade whose round-trip commission (bps included, base fees) exceeds 0.45 of
  its real risk is VETOED. The veto uses base fees, so base and stress evaluate exactly the
  same trades;
- unchanged: the 6 candidates, the thresholds, the gate, the stress scenario (fees x 1.5,
  +0.2 pip), and no extra search or recalibration after results.
"""

import argparse
import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.candles.engine import CandlestickEngine
from app.fx.agents.trained import (
    FEATURE_NAMES,
    RUN_SCHEMA,
    TIMEFRAME_MINUTES,
    ForestModel,
    LinearModel,
    feature_frame,
    signals_from_features,
)
from app.fx.models import pair
from app.fx.research.backtest import EUR_TO, USD_TO, _ns, _trading_day_ordinal, summarize
from app.fx.research.history import with_spread_floor

SCHEMA = "fx-m5-pipeline-validation/1"
TIMEFRAME = "M5"
BAR = pd.Timedelta(minutes=TIMEFRAME_MINUTES[TIMEFRAME])
ROLES: dict[str, tuple[str, str]] = {  # specialist -> (candle family, auxiliary algorithm)
    "reversao": ("reversal", "ridge"),
    "continuacao": ("continuation", "forest"),
    "impulso": ("momentum", "logistic"),
}


@dataclass(frozen=True)
class LabCosts:
    capital_eur: float = 10_000.0
    risk_pct: float = 0.1
    max_units: float = 5_000.0  # EUR (the base currency of every lab pair)
    unit_step: float = 1_000.0
    commission_bps: float = 0.2  # per side
    commission_min_usd: float = 2.0  # per side (per order)
    financing_bps_per_night: float = 0.5
    slippage_pips: float = 0.0  # per fill
    cost_multiplier: float = 1.0


BASE_COSTS = LabCosts()
STRESS_COSTS = replace(BASE_COSTS, slippage_pips=0.2, cost_multiplier=1.5)


@dataclass(frozen=True)
class M5Protocol:
    pairs: tuple[str, ...] = ("EURUSD", "EURGBP", "EURJPY")
    train_start: str = "2023-01-01T00:00:00+00:00"
    folds: tuple[tuple[str, str, str], ...] = (  # name, validation start (= train end), end
        ("2024", "2024-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
        ("2025", "2025-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    holdout_start: str = "2026-01-01T00:00:00+00:00"  # temporal holdout; data end = its end
    horizon_bars: int = 96  # label horizon = time stop (8 hours), amendment 1
    embargo_hours: float = 24.0
    gap_minutes: float = 30.0  # longer holes (weekends) never span a label or a trade
    min_training_rows: int = 5_000
    variants: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "ridge": (1.0, 100.0),  # alpha
            "forest": (200.0, 1_000.0),  # min_samples_leaf
            "logistic": (0.1, 1.0),  # C
        }
    )
    thresholds: dict[str, float] = field(
        default_factory=lambda: {"ridge": 0.35, "forest": 0.2, "logistic": 0.55}
    )
    forest_trees: int = 32
    forest_depth: int = 4
    seed: int = 7
    stop_atr: float = 2.0
    target_rr: float = 2.0  # target = 2 x stop distance (amendment 1)
    max_fee_risk: float = 0.45  # round-trip commission / real risk, at most (amendment 1)
    min_pattern_strength: float = 0.5
    require_model_agreement: bool = True
    sell_only: bool = True
    max_spread_pips: float = 3.0
    max_reversal_efficiency: float = 0.65
    trend_efficiency: float = 0.25  # analyst regime label only
    max_volatility_rank: float = 0.98
    max_entries_per_day: int = 5
    max_open_positions: int = 3
    gate_min_closed: int = 60
    gate_min_positive_periods: int = 2
    gate_min_profit_factor: float = 1.1
    gate_min_mean_r: float = 0.0  # strictly greater
    gate_min_stress_mean_r: float = 0.0  # strictly greater
    holdout_min_trades: int = 20
    holdout_min_total_r: float = 0.0  # strictly greater
    base_costs: LabCosts = BASE_COSTS
    stress_costs: LabCosts = STRESS_COSTS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["feature_names"] = list(FEATURE_NAMES)
        d["roles"] = {k: list(v) for k, v in ROLES.items()}
        d["schema"] = SCHEMA
        d["amendment"] = {
            "id": 1,
            "basis": "ex ante: 2 USD minimum per order and trade geometry only; no performance "
            "consulted, no training run before it",
            "reason": "old protocol infeasible: ~1.33 R of minimum commission per trade "
            "(2 ATR M5 stop ~6 pips, 5 000 EUR cap)",
            "superseded": {"horizon_bars": 12, "stop": "2 ATR", "target": "3 ATR"},
            "current": {
                "horizon_bars": self.horizon_bars,
                "stop": "max(2 ATR, 2*min_commission_quote/max_fee_risk/max_units)",
                "target": f"{self.target_rr:g} x stop",
                "fee_veto": f"round-trip commission / real risk > {self.max_fee_risk}",
            },
            "unchanged": "6 candidates, thresholds, gate, stress scenario; no extra search",
        }
        d["holdout_note"] = (
            "temporal M5 holdout; 2026 was seen by earlier M15 research, so this is not "
            "absolute statistical independence"
        )
        return d


PROTOCOL = M5Protocol()


def digest(data: Any) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, allow_nan=False, default=str).encode()
    ).hexdigest()


def strict_json(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: strict_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [strict_json(v) for v in value]
    return value


def write_json(path: Path, data: Any) -> str:
    clean = strict_json(data)
    path.write_text(json.dumps(clean, indent=2, allow_nan=False, default=str), encoding="utf-8")
    return digest(clean)


# --- data ---------------------------------------------------------------------------------


def check_manifest(root: Path, pairs: tuple[str, ...] = PROTOCOL.pairs) -> dict[str, Any]:
    """The downloader's manifest must be complete, genuine M5 (never upsampled) and match
    the files on disk exactly: every chunk present with its SHA-256 and row count, and no
    unlisted chunk file."""
    path = root / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"{path} missing: the M5 download is not finished")
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("complete") is not True:
        raise ValueError("M5 manifest is not complete: wait for the downloader")
    if manifest.get("upsampled") is not False or manifest.get("bar_size") != "5 mins":
        raise ValueError("M5 manifest is not genuine 5-minute data")
    listed: set[Path] = set()
    for chunk in manifest.get("chunks", []):
        name = Path(str(chunk["file"]).replace("\\", "/")).name
        f = root / chunk["pair"] / name
        if not f.is_file():
            raise FileNotFoundError(f"manifest chunk missing on disk: {f}")
        if hashlib.sha256(f.read_bytes()).hexdigest() != chunk["sha256"]:
            raise ValueError(f"SHA-256 mismatch for {f}")
        if len(pd.read_parquet(f)) != int(chunk["rows"]):
            raise ValueError(f"row count mismatch for {f}")
        listed.add(f.resolve())
    for symbol in pairs:
        for side in ("BID", "ASK"):
            files = {f.resolve() for f in (root / symbol).glob(f"{side}_M5_*.parquet")}
            if not files:
                raise ValueError(f"no {side} chunks for {symbol}")
            extra = files - listed
            if extra:
                raise ValueError(f"chunk files not in the manifest: {sorted(map(str, extra))}")
    return manifest


def load_m5(root: Path, symbol: str, before: pd.Timestamp | None = None) -> pd.DataFrame:
    """BID/ASK M5 OHLC (weekly/quarterly chunk files), overlaps deduplicated, spread floor
    applied. `before` truncates the data (the holdout is not even loaded before selection)."""
    sides = {}
    for side in ("BID", "ASK"):
        files = sorted((root / symbol).glob(f"{side}_M5_*.parquet"))
        if not files:
            raise FileNotFoundError(f"no {side} M5 data for {symbol} under {root}")
        from app.fx.research.m5_history import validate

        df = pd.concat([validate(pd.read_parquet(f)) for f in files], ignore_index=True)
        df = validate(df)  # native M5 alignment, finite OHLC, overlaps deduplicated
        df = df.set_index("ts").sort_index()
        if before is not None:
            df = df[df.index < before]
        sides[side] = (
            df[["open", "high", "low", "close"]].astype(float).add_prefix(f"{side.lower()}_")
        )
    joined = sides["BID"].join(sides["ASK"], how="inner")
    return with_spread_floor(joined, symbol)


def mid_of(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {k: (df[f"bid_{k}"] + df[f"ask_{k}"]) / 2 for k in ("open", "high", "low", "close")},
        index=df.index,
    )


# --- analyst and specialists ----------------------------------------------------------------


def pattern_frame(mid: pd.DataFrame) -> pd.DataFrame:
    """Per bar and family: strongest bullish and bearish real candle pattern (context met).
    Columns `<family>_bull`, `<family>_bear` (strength, 0 when absent). Causal: a pattern
    is stamped on the open time of its LAST (closed) candle."""
    obs = CandlestickEngine().analyze(mid.assign(volume=0.0), timeframe=TIMEFRAME).observations
    out = pd.DataFrame(index=mid.index)
    for family, _ in ROLES.values():
        for orient, name in (("bullish", "bull"), ("bearish", "bear")):
            sel = obs[
                (obs["family"] == family)
                & (obs["orientation"] == orient)
                & obs["context_requirements_met"].astype(bool)
            ]
            best = sel.groupby("ts")["strength"].max() if len(sel) else pd.Series(dtype=float)
            out[f"{family}_{name}"] = best.reindex(mid.index).fillna(0.0).to_numpy(float)
    return out


def analyst_frame(f: pd.DataFrame, protocol: M5Protocol = PROTOCOL) -> pd.DataFrame:
    """The market analyst's context on each closed bar (same rules as the paper lab)."""
    finite = np.isfinite(f[["atr", "er", "volatility_rank", "sma_gap"]].to_numpy(float)).all(axis=1)
    trend = np.sign(f["sma_gap"].fillna(0.0).to_numpy(float)).astype(int)
    tradable = (
        finite
        & f["tradable"].to_numpy(bool)
        & (f["volatility_rank"].fillna(1.0).to_numpy(float) < protocol.max_volatility_rank)
    )
    return pd.DataFrame(
        {
            "trend": trend,
            "efficiency": f["er"].to_numpy(float),
            "regime": np.where(f["er"].to_numpy(float) >= protocol.trend_efficiency, 1, 0),
            "tradable": tradable,
            "atr": f["atr"].to_numpy(float),
        },
        index=f.index,
    )


def fee_floor(symbol: str, protocol: M5Protocol = PROTOCOL) -> float:
    """Smallest stop distance at which the round-trip minimum commission is at most
    `max_fee_risk` R with the full size cap (price units of the quote currency)."""
    c = protocol.base_costs
    min_q = c.commission_min_usd * USD_TO.get(pair(symbol).quote, 1.0)
    return 2 * min_q / protocol.max_fee_risk / c.max_units


def fee_ratio(
    symbol: str, units: float, entry: float, stop_dist: float, costs: LabCosts = BASE_COSTS
) -> float:
    """Ex-ante round-trip commission (both sides at the entry price, bps included) over the
    real risk of `units`."""
    min_q = costs.commission_min_usd * USD_TO.get(pair(symbol).quote, 1.0)
    side = max(min_q, costs.commission_bps * 1e-4 * units * entry)
    return 2 * side / (units * stop_dist)


def pipeline_signals(
    df: pd.DataFrame,
    f: pd.DataFrame,
    patterns: pd.DataFrame,
    role: str,
    model: LinearModel,
    symbol: str,
    protocol: M5Protocol = PROTOCOL,
) -> pd.DataFrame:
    """The full specialist pipeline on every closed bar: dir (-1/0), stop/target distances,
    score, max_hold. Bullish choices are vetoed (never inverted) in SELL-only mode."""
    family = ROLES[role][0]
    a = analyst_frame(f, protocol)
    bull = patterns[f"{family}_bull"].to_numpy(float)
    bear = patterns[f"{family}_bear"].to_numpy(float)
    trend = a["trend"].to_numpy(int)
    if role == "reversao":
        # strongest pattern of the family whatever its side; a tie never becomes a SELL
        direction = np.where(bear > bull, -1, np.where(bull > 0, 1, 0))
        strength = np.maximum(bull, bear)
        allowed = a["efficiency"].fillna(1.0).to_numpy(float) <= protocol.max_reversal_efficiency
    else:  # continuation / momentum must follow the analyst's trend
        direction = np.where((trend < 0) & (bear > 0), -1, np.where((trend > 0) & (bull > 0), 1, 0))
        strength = np.where(direction < 0, bear, np.where(direction > 0, bull, 0.0))
        allowed = np.ones(len(f), bool)
    forecast = signals_from_features(f, model)["dir"].to_numpy(int)
    agree = forecast == direction
    spread = (df["ask_close"] - df["bid_close"]).to_numpy(float) / pair(symbol).pip_size
    ok = (
        a["tradable"].to_numpy(bool)
        & allowed
        & (direction != 0)
        & (strength >= protocol.min_pattern_strength)
        & (spread <= protocol.max_spread_pips)
        & (agree if protocol.require_model_agreement else True)
    )
    if protocol.sell_only:
        ok &= direction == -1
    atr = a["atr"].to_numpy(float)
    stop = np.maximum(protocol.stop_atr * atr, fee_floor(symbol, protocol))
    score = np.minimum(0.95, 0.56 + 0.24 * strength + np.where(agree, 0.1, 0.0))
    return pd.DataFrame(
        {
            "dir": np.where(ok, direction, 0),
            "stop_dist": stop,
            "target_dist": protocol.target_rr * stop,
            "score": np.where(ok, score, 0.0),
            "max_hold": protocol.horizon_bars,
        },
        index=f.index,
    )


# --- training -----------------------------------------------------------------------------


def training_rows(
    frames: dict[str, pd.DataFrame],
    features: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    before: pd.Timestamp,
    protocol: M5Protocol = PROTOCOL,
) -> tuple[np.ndarray, np.ndarray]:
    """Rows from [start, before): label = (close at t+H - open at t+1) / ATR. A label whose
    end (t+H bar close) is not before `before - embargo`, or that spans a data gap, is
    purged (purge = horizon + 1 bars)."""
    h = protocol.horizon_bars
    xs, ys = [], []
    for symbol, df in frames.items():
        mid = mid_of(df)
        f = features[symbol]
        y = ((mid["close"].shift(-h) - mid["open"].shift(-1)) / f["atr"]).clip(-20, 20)
        index = pd.Series(pd.DatetimeIndex(df.index), index=df.index)
        label_end = index.shift(-h) + BAR
        contiguous = (label_end - index) == (h + 1) * BAR
        cutoff = before - pd.Timedelta(hours=protocol.embargo_hours)
        mask = (index >= start) & (label_end < cutoff) & contiguous
        x = f[list(FEATURE_NAMES)].to_numpy(float)
        valid = mask.to_numpy(bool) & np.isfinite(x).all(axis=1) & np.isfinite(y.to_numpy())
        xs.append(x[valid])
        ys.append(y.to_numpy(float)[valid])
    joined_x, joined_y = np.concatenate(xs), np.concatenate(ys)
    if len(joined_y) < protocol.min_training_rows:
        raise ValueError(f"insufficient purged training rows ({len(joined_y)})")
    return joined_x, joined_y


def fit_model(
    role: str,
    variant: float,
    x: np.ndarray,
    y: np.ndarray,
    cutoff: pd.Timestamp,
    tag: str,
    protocol: M5Protocol = PROTOCOL,
) -> LinearModel:
    from sklearn.ensemble import ExtraTreesRegressor
    from sklearn.linear_model import LogisticRegression, Ridge

    algorithm = ROLES[role][1]
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale = np.where(scale > 1e-8, scale, 1.0)
    z = (x - center) / scale
    threshold = protocol.thresholds[algorithm]
    model_id = f"m5-{role}-{algorithm}-{variant:g}-{tag}"
    common: dict[str, Any] = {
        "stop_atr": protocol.stop_atr,
        "target_atr": protocol.target_rr * protocol.stop_atr,  # record only: see fee_floor
        "max_hold": protocol.horizon_bars,
        "feature_names": FEATURE_NAMES,
    }
    if algorithm == "forest":
        forest = ExtraTreesRegressor(
            n_estimators=protocol.forest_trees,
            max_depth=protocol.forest_depth,
            min_samples_leaf=int(variant),
            max_features=0.8,
            n_jobs=1,
            random_state=protocol.seed,
        ).fit(z, y)
        trees: tuple[dict[str, list[Any]], ...] = tuple(
            {
                "left": [int(v) for v in t.tree_.children_left],
                "right": [int(v) for v in t.tree_.children_right],
                "feature": [int(v) for v in t.tree_.feature],
                "threshold": [float(v) for v in t.tree_.threshold],
                "value": [float(v) for v in t.tree_.value[:, 0, 0]],
            }
            for t in forest.estimators_
        )
        return ForestModel(
            model_id,
            "forest",
            TIMEFRAME,
            tuple(center),
            tuple(scale),
            (0.0,) * len(FEATURE_NAMES),
            0.0,
            threshold,
            cutoff.isoformat(),
            trees=trees,
            **common,
        )
    if algorithm == "ridge":
        fitted = Ridge(alpha=variant).fit(z, y)
        coef, intercept = fitted.coef_, float(fitted.intercept_)
    else:
        logit = LogisticRegression(C=variant, max_iter=300, random_state=protocol.seed)
        logit.fit(z, y > 0)
        coef, intercept = logit.coef_[0], float(logit.intercept_[0])
    return LinearModel(
        model_id,
        algorithm,
        TIMEFRAME,
        tuple(center),
        tuple(scale),
        tuple(float(c) for c in coef),
        intercept,
        threshold,
        cutoff.isoformat(),
        **common,
    )


# --- lab simulation -----------------------------------------------------------------------


@dataclass
class _Arrays:
    ts: pd.DatetimeIndex
    ns: np.ndarray  # epoch nanoseconds (resolution-safe)
    o: dict[str, np.ndarray]
    h: dict[str, np.ndarray]
    lo: dict[str, np.ndarray]
    c: dict[str, np.ndarray]
    gap_after: np.ndarray  # True when the next bar is missing or far away
    day: np.ndarray  # FX trading day (17:00 New York)


def _arrays(df: pd.DataFrame, protocol: M5Protocol) -> _Arrays:
    ts = pd.DatetimeIndex(df.index)
    ns = _ns(ts)
    gap = np.ones(len(ts), bool)
    if len(ts) > 1:
        gap[:-1] = np.diff(ns) > int(protocol.gap_minutes * 60e9)
    get = {
        s: {k: df[f"{s}_{k}"].to_numpy(float) for k in ("open", "high", "low", "close")}
        for s in ("bid", "ask")
    }
    return _Arrays(
        ts,
        ns,
        {s: get[s]["open"] for s in get},
        {s: get[s]["high"] for s in get},
        {s: get[s]["low"] for s in get},
        {s: get[s]["close"] for s in get},
        gap,
        _trading_day_ordinal(ts),
    )


def _exit(
    a: _Arrays,
    d: int,
    e: int,
    stop: float,
    target: float,
    deadline: int,
    slip: float,
    bound: int | None = None,
) -> tuple[int, float, str]:
    """First exit from bar `e` on: stop before target in the same bar (conservative), gaps
    at the open, a hole in the data, the time stop at the close of `deadline`, or a forced
    close at the last bar of the evaluation window (`bound`): no exit uses later data."""
    side = "bid" if d > 0 else "ask"  # a long closes on the bid, a short on the ask
    o, hi, lo, cl = a.o[side], a.h[side], a.lo[side], a.c[side]
    last = len(a.ns) - 1 if bound is None else min(bound, len(a.ns) - 1)
    end = min(deadline, last)
    for k in range(e, end + 1):
        if d > 0:
            if k > e and o[k] <= stop:
                return k, float(o[k]) - slip, "stop_gap"
            if lo[k] <= stop:
                return k, stop - slip, "stop"
            if k > e and o[k] >= target:
                return k, float(o[k]) - slip, "target_gap"
            if hi[k] >= target:
                return k, target - slip, "target"
        else:
            if k > e and o[k] >= stop:
                return k, float(o[k]) + slip, "stop_gap"
            if hi[k] >= stop:
                return k, stop + slip, "stop"
            if k > e and o[k] <= target:
                return k, float(o[k]) + slip, "target_gap"
            if lo[k] <= target:
                return k, target + slip, "target"
        if a.gap_after[k] and k < end:
            return k, float(cl[k]) - d * slip, "gap"
    if end == deadline:
        return end, float(cl[end]) - d * slip, "time"
    return end, float(cl[end]) - d * slip, "boundary" if bound is not None else "end"


def simulate(
    frames: dict[str, pd.DataFrame],
    signals: dict[str, pd.DataFrame],
    lo: pd.Timestamp,
    hi: pd.Timestamp,
    costs: LabCosts,
    protocol: M5Protocol = PROTOCOL,
) -> pd.DataFrame:
    """One specialist across the lab pairs with the lab limits: one position per pair, at
    most `max_open_positions` at once and `max_entries_per_day` entries per FX trading day.
    Signals at a bar's close enter at the next bar's open (bid for SELL, ask for BUY)."""
    arrays = {s: _arrays(df, protocol) for s, df in frames.items()}
    lo_ns, hi_ns = int(_ns(pd.DatetimeIndex([lo]))[0]), int(_ns(pd.DatetimeIndex([hi]))[0])
    sig_arrays: dict[str, dict[str, np.ndarray]] = {
        s: {
            k: sig[k].to_numpy(float)
            for k in ("dir", "stop_dist", "target_dist", "score", "max_hold")
        }
        for s, sig in signals.items()
    }
    events: list[tuple[int, float, str, int]] = []
    bar_ns = int(BAR.total_seconds() * 1e9)
    bounds: dict[str, int] = {}
    for s, sa in sig_arrays.items():
        a = arrays[s]
        bounds[s] = int(np.searchsorted(a.ns, hi_ns)) - 1  # last bar opening before `hi`
        exact_next = np.zeros(len(a.ns), bool)
        exact_next[:-1] = np.diff(a.ns) == bar_ns  # a missing bar never allows an entry
        live = (sa["dir"] != 0) & (a.ns >= lo_ns) & (a.ns < hi_ns) & exact_next
        live[bounds[s] :] = False  # the entry bar itself must lie inside the window
        for j in np.flatnonzero(live).tolist():
            events.append((int(a.ns[j]), -float(sa["score"][j]), s, int(j)))
    events.sort()
    open_until: dict[str, int] = {}  # pair -> exit bar close (ns)
    per_day: dict[int, int] = {}
    rows: list[dict[str, Any]] = []
    for _, _, s, j in events:
        a, sa = arrays[s], sig_arrays[s]
        e = j + 1
        entry_ns = int(a.ns[e])
        open_until = {k: v for k, v in open_until.items() if v > entry_ns}
        day = int(a.day[e])
        if s in open_until or len(open_until) >= protocol.max_open_positions:
            continue
        if per_day.get(day, 0) >= protocol.max_entries_per_day:
            continue
        d = int(sa["dir"][j])
        sdist, tdist = float(sa["stop_dist"][j]), float(sa["target_dist"][j])
        if not (math.isfinite(sdist) and sdist > 0 and math.isfinite(tdist) and tdist > 0):
            continue
        p = pair(s)
        slip = costs.slippage_pips * p.pip_size
        entry = float(a.o["ask"][e] if d > 0 else a.o["bid"][e]) + d * slip
        risk_target_q = costs.capital_eur * costs.risk_pct / 100 * EUR_TO.get(p.quote, 1.0)
        units = min(risk_target_q / sdist, costs.max_units)
        units = math.floor(units / costs.unit_step) * costs.unit_step
        if units < costs.unit_step:
            continue  # never forced up to the minimum size
        if fee_ratio(s, units, entry, sdist, protocol.base_costs) > protocol.max_fee_risk + 1e-9:
            continue  # vetoed ex ante on BASE fees, so base and stress see the same trades
        stop, target = entry - d * sdist, entry + d * tdist
        deadline = e + int(sa["max_hold"][j]) - 1
        k, px, why = _exit(a, d, e, stop, target, deadline, slip, bounds[s])
        risk_q = units * sdist
        min_q = costs.commission_min_usd * USD_TO.get(p.quote, 1.0)
        side_comm = [max(min_q, costs.commission_bps * 1e-4 * units * x) for x in (entry, px)]
        nights = int(a.day[k] - a.day[e])
        fin_q = nights * costs.financing_bps_per_night * 1e-4 * units * entry
        cost_q = (sum(side_comm) + fin_q) * costs.cost_multiplier
        r_gross = d * (px - entry) / sdist
        r_cost = cost_q / risk_q
        open_until[s] = int(a.ns[k]) + bar_ns
        per_day[day] = per_day.get(day, 0) + 1
        rows.append(
            {
                "pair": s,
                "dir": d,
                "signal_ts": a.ts[j],
                "entry_ts": a.ts[e],
                "exit_ts": a.ts[k],
                "entry": entry,
                "exit": px,
                "units": units,
                "stop_dist": sdist,
                "reason": why,
                "bars": k - e + 1,
                "nights": nights,
                "r_gross": r_gross,
                "r_cost": r_cost,
                "r_net": r_gross - r_cost,
            }
        )
    return pd.DataFrame(rows)


# --- gate ---------------------------------------------------------------------------------


def gate(
    periods: dict[str, pd.DataFrame], stressed: pd.DataFrame, protocol: M5Protocol = PROTOCOL
) -> dict[str, Any]:
    """The frozen validation gate on the pooled validation folds."""
    pooled = (
        pd.concat([t for t in periods.values() if len(t)], ignore_index=True)
        if any(len(t) for t in periods.values())
        else pd.DataFrame(columns=["r_net", "r_cost", "bars"])
    )
    s = summarize(pooled)
    st = summarize(stressed) if len(stressed) else summarize(pooled.iloc[0:0])
    positive = sum(1 for t in periods.values() if len(t) and float(t["r_net"].mean()) > 0)
    checks = {
        "min_closed": s["trades"] >= protocol.gate_min_closed,
        "positive_periods": positive >= protocol.gate_min_positive_periods,
        "profit_factor": s["profit_factor"] >= protocol.gate_min_profit_factor,
        "mean_r": s["mean_r"] > protocol.gate_min_mean_r,
        "stress_mean_r": st["mean_r"] > protocol.gate_min_stress_mean_r,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "pooled": s,
        "stressed": st,
        "positive_periods": positive,
        "per_period": {
            k: summarize(v) if len(v) else summarize(pooled.iloc[0:0]) for k, v in periods.items()
        },
    }


def holdout_eligible(trades: pd.DataFrame, protocol: M5Protocol = PROTOCOL) -> dict[str, Any]:
    s = summarize(trades) if len(trades) else summarize(pd.DataFrame(columns=["r_net"]))
    ok = s["trades"] >= protocol.holdout_min_trades and s["total_r"] > protocol.holdout_min_total_r
    return {"eligible_demo": bool(ok), "summary": s}


# --- runner -------------------------------------------------------------------------------

Loader = Callable[[Path, str, pd.Timestamp | None], pd.DataFrame]


def _prepare(frames: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], ...]:
    features = {s: feature_frame(mid_of(df), TIMEFRAME) for s, df in frames.items()}
    patterns = {s: pattern_frame(mid_of(df)) for s, df in frames.items()}
    return features, patterns


def _evaluate(
    frames: dict[str, pd.DataFrame],
    features: dict[str, pd.DataFrame],
    patterns: dict[str, pd.DataFrame],
    role: str,
    model: LinearModel,
    lo: pd.Timestamp,
    hi: pd.Timestamp,
    costs: LabCosts,
    protocol: M5Protocol,
) -> pd.DataFrame:
    signals = {
        s: pipeline_signals(frames[s], features[s], patterns[s], role, model, s, protocol)
        for s in frames
    }
    return simulate(frames, signals, lo, hi, costs, protocol)


def save_model(directory: Path, model: LinearModel) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    return write_json(directory / f"{model.model_id}.json", model.to_dict())


def save_trades(directory: Path, name: str, trades: pd.DataFrame) -> str:
    """Audit CSV (timestamps, units, prices, costs per trade); empty files are kept."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.csv"
    cols = [
        "pair",
        "dir",
        "signal_ts",
        "entry_ts",
        "exit_ts",
        "entry",
        "exit",
        "units",
        "stop_dist",
        "reason",
        "bars",
        "nights",
        "r_gross",
        "r_cost",
        "r_net",
    ]
    (trades if len(trades) else pd.DataFrame(columns=cols)).to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    data: Path,
    out: Path,
    protocol: M5Protocol = PROTOCOL,
    loader: Loader = load_m5,
    require_manifest: bool = True,
    say: Callable[[str], None] = print,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    if (out / "holdout.json").exists():
        raise SystemExit("holdout already evaluated for this run: refusing to repeat it")
    # 1. protocol frozen BEFORE any market data is read
    protocol_sha = write_json(out / "protocol.json", protocol.to_dict())
    say(f"protocolo {protocol_sha[:12]} gravado")
    if require_manifest:
        manifest = check_manifest(data)
        write_json(out / "data_manifest.json", manifest)
    holdout_start = pd.Timestamp(protocol.holdout_start)
    start = pd.Timestamp(protocol.train_start)

    # 2. selection on 2023-2025 only (the holdout is not loaded)
    frames = {s: loader(data, s, holdout_start) for s in protocol.pairs}
    features, patterns = _prepare(frames)
    candidates: list[dict[str, Any]] = []
    for role, (_, algorithm) in ROLES.items():
        for variant in protocol.variants[algorithm]:
            periods: dict[str, pd.DataFrame] = {}
            stressed: list[pd.DataFrame] = []
            audit: list[dict[str, Any]] = []
            cand = f"{role}-{algorithm}-{variant:g}"
            for name, val_lo, val_hi in protocol.folds:
                lo_ts, hi_ts = pd.Timestamp(val_lo), pd.Timestamp(val_hi)
                x, y = training_rows(frames, features, start, lo_ts, protocol)
                model = fit_model(role, variant, x, y, lo_ts, f"f{name}", protocol)
                args = (frames, features, patterns, role, model, lo_ts, hi_ts)
                periods[name] = _evaluate(*args, protocol.base_costs, protocol)
                stressed.append(_evaluate(*args, protocol.stress_costs, protocol))
                audit.append(
                    {
                        "fold": name,
                        "model_id": model.model_id,
                        "training_rows": len(y),
                        "model_sha256": save_model(out / "models-folds", model),
                        "trades_base_sha256": save_trades(
                            out / "trades", f"{cand}__{name}__base", periods[name]
                        ),
                        "trades_stress_sha256": save_trades(
                            out / "trades", f"{cand}__{name}__stress", stressed[-1]
                        ),
                    }
                )
            st = [t for t in stressed if len(t)]
            result = gate(
                periods, pd.concat(st, ignore_index=True) if st else pd.DataFrame(), protocol
            )
            candidates.append(
                {
                    "candidate": cand,
                    "role": role,
                    "algorithm": algorithm,
                    "variant": variant,
                    "folds": audit,
                    **result,
                }
            )
            say(f"{role} {algorithm} {variant:g}: gate {'OK' if result['passed'] else 'falha'}")
    write_json(out / "candidates.json", {"protocol_sha256": protocol_sha, "candidates": candidates})
    selection: dict[str, Any] = {}
    for role in ROLES:
        passing = [c for c in candidates if c["role"] == role and c["passed"]]
        best = max(passing, key=lambda c: (c["pooled"]["mean_r"], -c["variant"]), default=None)
        selection[role] = None if best is None else best["candidate"]
    selection_sha = write_json(
        out / "selection.json",
        {
            "protocol_sha256": protocol_sha,
            "frozen_at": datetime.now(UTC).isoformat(),
            "selection": selection,
        },
    )

    # 3. temporal holdout, once, only for selected candidates
    holdout: dict[str, Any] = {"selection_sha256": selection_sha, "roles": {}}
    final_models: list[LinearModel] = []
    chosen = {r: c for r, c in selection.items() if c is not None}
    if chosen:
        full = {s: loader(data, s, None) for s in protocol.pairs}
        f_full, p_full = _prepare(full)
        end = max(pd.DatetimeIndex(df.index).max() for df in full.values()) + BAR
        for role, cand in chosen.items():
            c = next(x for x in candidates if x["candidate"] == cand)
            x, y = training_rows(full, f_full, start, holdout_start, protocol)
            model = fit_model(role, c["variant"], x, y, holdout_start, "final", protocol)
            trades = _evaluate(
                full, f_full, p_full, role, model, holdout_start, end, protocol.base_costs, protocol
            )
            holdout["roles"][role] = {
                "candidate": cand,
                "model_id": model.model_id,
                "model_sha256": save_model(out / "models-final", model),
                "trades_sha256": save_trades(out / "trades", f"{cand}__holdout__base", trades),
                **holdout_eligible(trades, protocol),
            }
            final_models.append(model)
    write_json(out / "holdout.json", holdout)
    eligible = [v["model_id"] for v in holdout["roles"].values() if v["eligible_demo"]]
    roles_of = {v["model_id"]: r for r, v in holdout["roles"].items()}
    registry = {
        "schema": RUN_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": protocol_sha,
        "selection_sha256": selection_sha,
        "statistically_approved": False,
        "research_status": "m5_walk_forward_temporal_holdout",
        "timeframe": TIMEFRAME,
        "models": [m.to_dict() for m in final_models],
        "active_model_ids": eligible[:3],
        "ranking": [
            {
                "model_id": m.model_id,
                "role": roles_of[m.model_id],
                "paper_candidate": m.model_id in eligible,
            }
            for m in final_models
        ],
        "pipelines": {
            m.model_id: {
                "role": roles_of[m.model_id],
                "family": ROLES[roles_of[m.model_id]][0],
                "sell_only": protocol.sell_only,
                "require_model_agreement": protocol.require_model_agreement,
                "min_pattern_strength": protocol.min_pattern_strength,
                "stop": "max(stop_atr x ATR, fee_floor)",
                "stop_atr": protocol.stop_atr,
                "fee_floor": {s: fee_floor(s, protocol) for s in protocol.pairs},
                "max_fee_risk": protocol.max_fee_risk,
                "target_rr": protocol.target_rr,
                "max_hold": protocol.horizon_bars,
                "max_spread_pips": protocol.max_spread_pips,
                "max_entries_per_day": protocol.max_entries_per_day,
                "max_open_positions": protocol.max_open_positions,
                # the coefficients alone are NOT what was validated: only this full pipeline
                "required_full_pipeline": True,
            }
            for m in final_models
        },
        "no_trade_roles": [
            r for r in ROLES if r not in roles_of or holdout["roles"][r]["model_id"] not in eligible
        ],
        "forward_paper_required": True,
    }
    write_json(out / "selected.json", registry)
    (out / "report.md").write_text(
        report(candidates, selection, holdout, protocol_sha), encoding="utf-8"
    )
    return registry


def report(
    candidates: list[dict[str, Any]],
    selection: dict[str, Any],
    holdout: dict[str, Any],
    protocol_sha: str,
) -> str:
    lines = [
        "# Validação M5 do pipeline analista -> especialista -> modelo (paper)",
        "",
        f"Protocolo `{protocol_sha[:12]}` gravado antes de ler dados. Holdout TEMPORAL 2026 "
        "(não é independência estatística absoluta: 2026 foi visto na pesquisa M15).",
        "",
        "| Candidato | Fechadas | Média R | PF | Períodos + | Stress média R | Gate |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in candidates:
        s, st = c["pooled"], c["stressed"]
        lines.append(
            f"| {c['candidate']} | {s['trades']:.0f} | {s['mean_r']:+.3f} | "
            f"{s['profit_factor']:.2f} | {c['positive_periods']} | {st['mean_r']:+.3f} | "
            f"{'passa' if c['passed'] else 'falha'} |"
        )
    lines += ["", "## Seleção (congelada antes do holdout)", ""]
    lines += [f"- {r}: {c or 'NO TRADE'}" for r, c in selection.items()]
    lines += ["", "## Holdout temporal 2026", ""]
    if not holdout["roles"]:
        lines.append("Nenhum candidato passou o gate: holdout não avaliado. NO TRADE.")
    for r, v in holdout["roles"].items():
        s = v["summary"]
        lines.append(
            f"- {r}: {s['trades']:.0f} operações, total {s['total_r']:+.2f} R -> "
            f"{'elegível para demo (experimental)' if v['eligible_demo'] else 'NO TRADE'}"
        )
    lines += [
        "",
        "## Limites da avaliação",
        "",
        "- Os limites do laboratório (5 entradas/dia, 3 posições, 1 por par) foram aplicados "
        "POR CANDIDATO, isoladamente. Isto NÃO é uma simulação fiel da carteira conjunta dos "
        "3 agentes; se mais de um for elegível, é preciso um teste conjunto antes de operar.",
        "- Cada operação termina dentro do seu período (fecho forçado na fronteira); uma "
        "entrada exige a barra seguinte exatamente 5 min depois.",
        "- O modelo auxiliar tem de concordar com o padrão, e o padrão tem força mínima; só "
        "SELL (conta financiada em EUR); padrões de alta são vetados, nunca invertidos.",
        "",
        "Nada aqui aprova dinheiro real; candidatos reprovados nunca são promovidos.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M5 pipeline walk-forward validation")
    ap.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    registry = run(args.data, args.out)
    print("ativos (experimental, paper):", registry["active_model_ids"] or "NENHUM (NO TRADE)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
