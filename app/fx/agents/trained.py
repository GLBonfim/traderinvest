"""Portable trained challengers. Numeric JSON only; no pickle or training at inference.

These models are experimental paper candidates, never statistical approval for real money.
The exact same causal feature/signal functions are used in the walk-forward tournament.
"""

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.candles.config import CandleConfig
from app.candles.engine import CandlestickEngine
from app.candles.geometry import compute_geometry
from app.candles.patterns import REGISTRY
from app.fx.agents.base import MarketView, from_signal, hold
from app.fx.agents.signals import session_flags
from app.fx.proposals import AgentProposal, AgentRole

BASE_FEATURE_NAMES = (
    "move1",
    "move4",
    "move16",
    "sma_gap",
    "distance",
    "rsi_centered",
    "efficiency",
    "volatility_rank",
    "hour_sin",
    "hour_cos",
)
CANDLE_GEOMETRY = (
    "body_ratio",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "close_position",
    "trend_score",
)
FEATURE_NAMES = (
    BASE_FEATURE_NAMES + CANDLE_GEOMETRY + tuple("candle_" + spec.name for spec, _ in REGISTRY)
)
MODEL_SCHEMA = "fx-portable-paper/2"
RUN_SCHEMA = "fx-training-run/1"
BAR_HOURS = {"H1": 1, "H4": 4}  # legacy H1/H4 trainer, pilot and resampling: do not extend
# every timeframe a portable model may declare; M5 is research-only until validated
TIMEFRAME_MINUTES = {"M5": 5, "H1": 60, "H4": 240}
RETIREMENT_POLICY: dict[str, Any] = {
    "minimum_closed_trades": 20,
    "rolling_trades": 50,
    "max_drawdown_r": 5.0,
}


@dataclass(frozen=True)
class LinearModel:
    model_id: str
    algorithm: str
    timeframe: str
    center: tuple[float, ...]
    scale: tuple[float, ...]
    weights: tuple[float, ...]
    intercept: float
    threshold: float
    training_end: str
    stop_atr: float = 2.0
    target_atr: float = 3.0
    max_hold: int = 8
    schema: str = MODEL_SCHEMA
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def __post_init__(self) -> None:
        if self.schema not in (MODEL_SCHEMA, "fx-linear-paper/1") or self.algorithm not in (
            "ridge",
            "logistic",
            "forest",
        ):
            raise ValueError("unsupported model schema/algorithm")
        if self.timeframe not in TIMEFRAME_MINUTES or not self.model_id:
            raise ValueError("invalid model identity/timeframe")
        if (
            not self.feature_names
            or len(set(self.feature_names)) != len(self.feature_names)
            or not set(self.feature_names) <= set(FEATURE_NAMES)
        ):
            raise ValueError("invalid feature names/version")
        if any(len(x) != len(self.feature_names) for x in (self.center, self.scale, self.weights)):
            raise ValueError("model feature dimensions differ")
        nums = (
            *self.center,
            *self.scale,
            *self.weights,
            self.intercept,
            self.threshold,
            self.stop_atr,
            self.target_atr,
        )
        if not all(math.isfinite(x) for x in nums) or any(x <= 0 for x in self.scale):
            raise ValueError("non-finite model or nonpositive scale")
        if self.stop_atr <= 0 or self.target_atr <= 0 or not 1 <= self.max_hold <= 96:
            raise ValueError("invalid model risk geometry")
        if self.threshold <= 0 or (self.algorithm == "logistic" and not 0.5 < self.threshold < 1):
            raise ValueError("invalid model threshold")
        stamp = pd.Timestamp(self.training_end)
        if stamp.tzinfo is None:
            raise ValueError("training_end needs an explicit timezone")

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "feature_names": list(self.feature_names)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LinearModel":
        data = dict(value)
        data["feature_names"] = tuple(data.get("feature_names", ()))
        for name in ("center", "scale", "weights"):
            data[name] = tuple(float(x) for x in data[name])
        if data["algorithm"] == "forest":
            data["trees"] = tuple(data["trees"])
            return ForestModel(**data)
        return cls(**data)


@dataclass(frozen=True)
class ForestModel(LinearModel):
    """Shallow ExtraTrees exported as bounded numeric arrays; never executable pickle."""

    trees: tuple[dict[str, list[Any]], ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.algorithm != "forest" or not 1 <= len(self.trees) <= 128:
            raise ValueError("invalid forest")
        for tree in self.trees:
            count = len(tree["left"])
            if not 1 <= count <= 1024 or any(
                len(tree[k]) != count for k in ("right", "feature", "threshold", "value")
            ):
                raise ValueError("invalid forest node arrays")
            if not all(math.isfinite(v) for k in ("threshold", "value") for v in tree[k]):
                raise ValueError("non-finite forest")
            for i, (left, right, feature) in enumerate(
                zip(tree["left"], tree["right"], tree["feature"], strict=True)
            ):
                if not all(isinstance(v, int) for v in (left, right, feature)):
                    raise ValueError("forest indices must be integers")
                if left == -1 and right == -1:
                    continue
                if not (
                    i < left < count
                    and i < right < count
                    and 0 <= feature < len(self.feature_names)
                ):
                    raise ValueError("cyclic or invalid forest node")


def forest_prediction(x: np.ndarray, trees: tuple[dict[str, list[Any]], ...]) -> np.ndarray:
    x = x.astype(np.float32)
    total = np.zeros(len(x))
    for tree in trees:
        left, right = np.asarray(tree["left"]), np.asarray(tree["right"])
        feature, threshold = np.asarray(tree["feature"]), np.asarray(tree["threshold"])
        nodes = np.zeros(len(x), dtype=int)
        # Acyclic child indices are checked on load; this bound also covers malformed depth.
        for _ in range(len(left)):
            active = left[nodes] != -1
            if not active.any():
                break
            rows = np.flatnonzero(active)
            current = nodes[rows]
            go_left = x[rows, feature[current]] <= threshold[current]
            nodes[rows] = np.where(go_left, left[current], right[current])
        total += np.asarray(tree["value"])[nodes]
    return total / len(trees)


def load_models(path: Path) -> list[LinearModel]:
    """Load only explicitly selected experimental models; never infer approval from a file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != RUN_SCHEMA or data.get("statistically_approved") is not False:
        raise ValueError("invalid experimental model registry")
    models = [LinearModel.from_dict(m) for m in data["models"]]
    by_id = {m.model_id: m for m in models}
    if len(by_id) != len(models):
        raise ValueError("duplicate model identifiers")
    selected = data["active_model_ids"]
    if len(selected) > 3 or len(set(selected)) != len(selected):
        raise ValueError("invalid active model selection")
    eligible = {r["model_id"] for r in data.get("ranking", []) if r.get("paper_candidate") is True}
    if not set(selected) <= eligible:
        raise ValueError("active selection contains an ineligible paper candidate")
    return [by_id[name] for name in selected]


def retirement_decision(
    registry: dict[str, Any],
    current_id: str,
    net_returns_r: list[float],
    retired_ids: list[str],
    active_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Retire deteriorating paper candidates; caller persists and deduplicates CLOSED fills.

    A five-R decline from the realized peak stops a candidate immediately. After 20 closed
    trades, a negative mean and PF below one over the last 50 trades also retires it. This is
    a predeclared operational rule, not a claim of statistical proof from 20 observations.
    If no eligible unretired challenger remains, replacement_id is None (NO TRADE).
    """
    if registry.get("schema") != RUN_SCHEMA or registry.get("statistically_approved") is not False:
        raise ValueError("invalid experimental registry")
    values = np.asarray(net_returns_r, dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("paper returns must be finite")
    curve = np.concatenate([[0.0], np.cumsum(values)])
    drawdown = float(np.maximum.accumulate(curve)[-1] - curve[-1])
    recent = values[-RETIREMENT_POLICY["rolling_trades"] :]
    mean = float(recent.mean()) if len(recent) else 0.0
    wins, losses = float(recent[recent > 0].sum()), float(-recent[recent < 0].sum())
    pf = wins / losses if losses > 0 else None
    reason = "continue paper observation"
    retire = current_id in retired_ids
    if retire:
        reason = "candidate already retired"
    elif drawdown >= RETIREMENT_POLICY["max_drawdown_r"]:
        retire, reason = True, "paper drawdown reached 5 R from the realized peak"
    elif len(values) >= RETIREMENT_POLICY["minimum_closed_trades"] and mean < 0 and losses > wins:
        retire, reason = (
            True,
            "negative rolling net return and profit factor after 20 closed trades",
        )
    replacement = None
    if retire:
        valid_ids = {m["model_id"] for m in registry["models"]}
        active = registry.get("active_model_ids", []) if active_ids is None else active_ids
        excluded = {*retired_ids, *active, current_id}
        replacement = next(
            (
                r["model_id"]
                for r in registry["ranking"]
                if r.get("paper_candidate") is True
                and r["model_id"] in valid_ids
                and r["model_id"] not in excluded
            ),
            None,
        )
    return {
        "retire": retire,
        "replacement_id": replacement,
        "reason": reason,
        "closed_trades": len(values),
        "net_r": float(values.sum()),
        "drawdown_r": drawdown,
        "rolling_mean_r": mean,
        "profit_factor": pf,
    }


def feature_frame(mid: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Closed-bar features. Sessions use the bar CLOSE time, not its opening label."""
    minutes = TIMEFRAME_MINUTES[timeframe]
    c = mid["close"]
    true_range = pd.concat(
        [mid["high"] - mid["low"], (mid["high"] - c.shift()).abs(), (mid["low"] - c.shift()).abs()],
        axis=1,
    ).max(axis=1)
    # Finite windows ensure research and a bounded production bar window agree exactly.
    atr = true_range.rolling(14).mean().where(lambda x: x > 0)
    changes = c.diff()
    gain, loss = (
        changes.clip(lower=0).rolling(14).mean(),
        (-changes.clip(upper=0)).rolling(14).mean(),
    )
    rsi = (100 * gain / (gain + loss)).where(gain + loss > 0, 50.0)
    efficiency = (c - c.shift(20)).abs() / changes.abs().rolling(20).sum().where(lambda x: x > 0)
    out = pd.DataFrame(index=mid.index)
    for lag in (1, 4, 16):
        out[f"move{lag}"] = ((c - c.shift(lag)) / atr).clip(-20, 20)
    out["sma_gap"] = ((c.rolling(20).mean() - c.rolling(60).mean()) / atr).clip(-20, 20)
    out["distance"] = ((c - c.rolling(20).mean()) / atr).clip(-20, 20)
    out["rsi_centered"] = (rsi - 50) / 50
    out["efficiency"] = efficiency
    out["volatility_rank"] = atr.rolling(200, min_periods=100).rank(pct=True)
    close_times = pd.DatetimeIndex(mid.index) + pd.Timedelta(minutes=minutes)
    angle = 2 * np.pi * (close_times.hour + close_times.minute / 60) / 24
    out["hour_sin"], out["hour_cos"] = np.sin(angle), np.cos(angle)
    # FX has no centralized traded volume: zero is an unavailable placeholder only.
    # No volume-derived column enters the model. Context precedes the first pattern bar.
    cfg = CandleConfig()
    geometry = compute_geometry(mid.assign(volume=0.0), cfg)
    for name in CANDLE_GEOMETRY:
        out[name] = geometry[name].clip(-20, 20)
    for spec, detector in REGISTRY:
        mask, strength = detector(geometry, cfg)
        context = CandlestickEngine._context(geometry, spec)
        out["candle_" + spec.name] = strength.where(mask & context, 0.0).fillna(0.0)
    out["atr"] = atr
    out["er"], out["rsi"] = efficiency, rsi
    sessions = session_flags(close_times)
    out["tradable"] = sessions["liquid"].to_numpy() & ~sessions["near_rollover"].to_numpy()
    return out


def model_signals(mid: pd.DataFrame, model: LinearModel) -> pd.DataFrame:
    return signals_from_features(feature_frame(mid, model.timeframe), model)


def signals_from_features(f: pd.DataFrame, model: LinearModel) -> pd.DataFrame:
    """`model_signals` on an already computed `feature_frame` (same timeframe as the model)."""
    x = f[list(model.feature_names)].to_numpy(float)
    valid = np.isfinite(x).all(axis=1) & np.isfinite(f["atr"].to_numpy(float))
    z = (x - np.asarray(model.center)) / np.asarray(model.scale)
    raw = (
        forest_prediction(z, model.trees)
        if isinstance(model, ForestModel)
        else z @ np.asarray(model.weights) + model.intercept
    )
    if model.algorithm == "logistic":
        value = 1 / (1 + np.exp(-np.clip(raw, -40, 40)))
        direction = np.where(
            value >= model.threshold, 1, np.where(value <= 1 - model.threshold, -1, 0)
        )
        strength = np.abs(value - 0.5) * 2
    else:
        direction = np.where(raw >= model.threshold, 1, np.where(raw <= -model.threshold, -1, 0))
        strength = np.minimum(np.abs(raw) / (3 * model.threshold), 1)
    direction = np.where(valid & f["tradable"].to_numpy(bool), direction, 0)
    return pd.DataFrame(
        {
            "dir": direction,
            "stop_dist": model.stop_atr * f["atr"],
            "target_dist": model.target_atr * f["atr"],
            "score": np.where(direction != 0, 0.55 + 0.4 * strength, 0.0),
            "max_hold": model.max_hold,
        },
        index=f.index,
    )


class TrainedAgent:
    """A replacement strategy in an existing specialist slot; its score is not win probability."""

    version = "3.0.0-experimental"
    min_bars = 260
    role: AgentRole

    def __init__(self, model: LinearModel, role: AgentRole = AgentRole.MOMENTUM) -> None:
        if role not in (AgentRole.TREND, AgentRole.MOMENTUM, AgentRole.MEAN_REVERSION):
            raise ValueError("a trained candidate needs a specialist role")
        self.model = model
        self.role = role

    def analyze(self, view: MarketView) -> AgentProposal:
        if view.timeframe != self.model.timeframe or len(view.bars) < self.min_bars:
            return hold(self, view, "candidato: dados/horizonte incompatíveis", {})
        if pd.Timestamp(view.now) < pd.Timestamp(self.model.training_end):
            return hold(self, view, "modelo treinado depois do instante de decisão", {})
        f = feature_frame(view.df, self.model.timeframe)
        row = model_signals(view.df, self.model).iloc[-1]
        result = from_signal(
            self,
            view,
            row,
            f.iloc[-1],
            "candidato treinado: alta",
            "candidato treinado: baixa",
            self.model.max_hold,
        )
        return result
