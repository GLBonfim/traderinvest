"""Contract between signal models (rule-based or trained) and their consumers: the bar
backtester (`app.fx.research.backtest.run_backtest`), the paper pilot (`app.fx.ibkr.pilot`)
and, later, the engine.

A `SignalModel` maps causal features (`app.fx.agents.signals.features`, one row per closed bar,
oldest first) to a signal frame with EXACTLY these columns, on the same index:

    dir          int    1 long, -1 short, 0 nothing (decided at that bar's close)
    stop_dist    float  > 0 price distance from the close to the stop when dir != 0
    target_dist  float  > 0 price distance to the target when dir != 0 (RR >= 1.2 recommended)
    score        float  in [0, 1], heuristic confidence (NOT a probability)
    max_hold     int    bars before the time stop when dir != 0

Rules: row t may use rows <= t only (no look-ahead); no I/O, network or randomness in
`signals`; deterministic for the same input. Models are loaded from JSON (no pickle).
`status` must say honestly what the model is: "approved" only after an independent,
pre-registered out-of-sample test; models selected on already-inspected history are
"pilot_candidate" and may only trade the PAPER account, clearly labelled as experimental.
"""

from typing import Literal, Protocol

import numpy as np
import pandas as pd

SIGNAL_COLUMNS = ("dir", "stop_dist", "target_dist", "score", "max_hold")
ModelStatus = Literal["approved", "pilot_candidate", "research", "retired"]


class SignalModel(Protocol):
    name: str
    version: str
    timeframe: str  # "M15" or "H1"
    status: ModelStatus
    min_bars: int

    def signals(self, f: pd.DataFrame) -> pd.DataFrame: ...


class SignalFrameError(ValueError):
    pass


def validate_signal_frame(sig: pd.DataFrame, index: pd.Index) -> pd.DataFrame:
    """Checks a model's output against the contract (raises SignalFrameError)."""
    missing = [c for c in SIGNAL_COLUMNS if c not in sig.columns]
    if missing:
        raise SignalFrameError(f"signal frame without columns {missing}")
    if not sig.index.equals(index):
        raise SignalFrameError("signal frame index differs from the feature index")
    d = sig["dir"].to_numpy()
    if not np.isin(d, (-1, 0, 1)).all():
        raise SignalFrameError("dir must be -1, 0 or 1")
    act = d != 0
    for col in ("stop_dist", "target_dist"):
        v = sig[col].to_numpy(float)[act]
        if not (np.isfinite(v) & (v > 0)).all():
            raise SignalFrameError(f"{col} must be finite and > 0 where dir != 0")
    sc = sig["score"].to_numpy(float)[act]
    if not ((sc >= 0) & (sc <= 1)).all():
        raise SignalFrameError("score must be in [0, 1]")
    mh = sig["max_hold"].to_numpy()[act]
    if not (mh >= 1).all():
        raise SignalFrameError("max_hold must be >= 1 where dir != 0")
    return sig


class RuleModel:
    """Adapter: one of the v2 rule agents' signal functions as a SignalModel."""

    def __init__(self, name: str, fn: object, params: object, status: ModelStatus) -> None:
        self.name, self.version, self.timeframe = name, "2.0.0", "M15"
        self.status: ModelStatus = status
        self.min_bars = 260
        self._fn, self._params = fn, params

    def signals(self, f: pd.DataFrame) -> pd.DataFrame:
        out: pd.DataFrame = self._fn(f, self._params)  # type: ignore[operator]
        return out
