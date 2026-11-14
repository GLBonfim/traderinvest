"""Native M5 bars and the shared, gated research-to-paper pipeline. No order API."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.agents.trained import LinearModel, feature_frame
from app.fx.research.history import with_spread_floor
from app.fx.research.m5_validation import (
    PROTOCOL,
    analyst_frame,
    digest,
    pattern_frame,
    pipeline_signals,
)

BAR = pd.Timedelta(minutes=5)
MAX_DELAY = pd.Timedelta(seconds=90)


def closed_native_m5(raw: pd.DataFrame, symbol: str, now: datetime) -> pd.DataFrame:
    """No resampling, interpolation or M15-as-M5; continuous latest 260 closed bars."""
    if not isinstance(raw.index, pd.DatetimeIndex) or raw.index.tz is None:
        raise ValueError("M5 timestamps require timezone")
    if not raw.index.is_unique or not raw.index.is_monotonic_increasing:
        raise ValueError("ordered unique M5 required")
    index = raw.index.tz_convert("UTC")
    if not ((index.minute % 5 == 0) & (index.second == 0)).all():
        raise ValueError("M5 grid required")
    out = raw.copy()
    out.index = index
    out = out.loc[index + BAR <= pd.Timestamp(now)]
    cols = [
        f"{side}_{field}" for side in ("bid", "ask") for field in ("open", "high", "low", "close")
    ]
    v = out[cols].to_numpy(float)
    if not np.isfinite(v).all() or (v <= 0).any():
        raise ValueError("invalid M5 prices")
    for side in ("bid", "ask"):
        if (
            (
                out[f"{side}_high"]
                < out[[f"{side}_open", f"{side}_close", f"{side}_low"]].max(axis=1)
            )
            | (out[f"{side}_low"] > out[[f"{side}_open", f"{side}_close"]].min(axis=1))
        ).any():
            raise ValueError("invalid OHLC")
    if any((out[f"ask_{f}"] < out[f"bid_{f}"]).any() for f in ("open", "high", "low", "close")):
        raise ValueError("crossed quotes")
    if len(out) < 260:
        return out.iloc[:0]
    tail = out.iloc[-260:]
    if not (tail.index.to_series().diff().iloc[1:] == BAR).all():
        return out.iloc[:0]
    if tail.index[-1] != pd.Timestamp(now).floor("5min") - BAR:
        return out.iloc[:0]
    return with_spread_floor(tail, symbol)


def assess(
    bars: pd.DataFrame,
    symbol: str,
    models: dict[str, LinearModel],
    roles: dict[str, str],
    protocol: Any = PROTOCOL,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from app.candles.htf_ltf_lesson import observe
    from app.fx.ibkr.pilot import mid_of

    mid = mid_of(bars)
    f = feature_frame(mid, "M5")
    patterns = pattern_frame(mid)
    a = analyst_frame(f).iloc[-1]
    context = {
        "timeframe": "M5",
        "bar_open": bars.index[-1].isoformat(),
        "bar_close": (bars.index[-1] + BAR).isoformat(),
        "tradable": bool(a.tradable),
        "trend": int(a.trend),
        "atr": float(a.atr),
        "patterns": patterns.iloc[-1].to_dict(),
        "eligible_models": list(models),
        "decisions": [],
    }
    proposals = []
    for mid_id, model in models.items():
        if model.timeframe != "M5" or bars.index[-1] + BAR < pd.Timestamp(model.training_end):
            continue
        row = pipeline_signals(bars, f, patterns, roles[mid_id], model, symbol, protocol).iloc[-1]
        context["decisions"].append(
            {"model_id": mid_id, "role": roles[mid_id], "direction": int(row["dir"])}
        )
        if int(row["dir"]) == -1:
            proposals.append(
                {"symbol": symbol, "model": model, "row": row, "bar_open": bars.index[-1]}
            )
    context["lesson_research"] = observe(mid, as_of=bars.index[-1] + BAR)
    if not models:
        context["no_trade_reason"] = "no_candidate_passed_frozen_validation"
    return context, proposals


def load_eligible(directory: Path) -> tuple[dict[str, LinearModel], dict[str, str]]:
    """Verify evidence chain, not merely a hand-edited list of active IDs."""
    registry = json.loads((directory / "selected.json").read_text())
    protocol = json.loads((directory / "protocol.json").read_text())
    selection = json.loads((directory / "selection.json").read_text())
    holdout = json.loads((directory / "holdout.json").read_text())
    if registry.get("timeframe") != "M5" or registry.get("statistically_approved") is not False:
        raise ValueError("experimental M5 registry required")
    if (
        digest(protocol) != registry["protocol_sha256"]
        or digest(selection) != registry["selection_sha256"]
    ):
        raise ValueError("evidence hash mismatch")
    if (
        selection["protocol_sha256"] != registry["protocol_sha256"]
        or holdout["selection_sha256"] != registry["selection_sha256"]
    ):
        raise ValueError("selection chain mismatch")
    protocol_from_hash(registry["protocol_sha256"])
    candidates = json.loads((directory / "candidates.json").read_text())
    if isinstance(candidates, dict):
        if candidates.get("protocol_sha256") != registry["protocol_sha256"]:
            raise ValueError("candidate protocol mismatch")
        candidates = candidates.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate list required")
    ids = registry.get("active_model_ids", [])
    if not ids:
        return {}, {}
    joint = json.loads((directory / "joint-paper-gate.json").read_text())
    if joint.get("registry_sha256") != digest(registry) or joint.get("passed") is not True:
        raise ValueError("joint portfolio/integration gate absent")
    models, roles = {}, {}
    for mid_id in ids:
        entries = [(r, h) for r, h in holdout["roles"].items() if h.get("model_id") == mid_id]
        if len(entries) != 1 or entries[0][1].get("eligible_demo") is not True:
            raise ValueError("holdout not passed")
        role, evidence = entries[0]
        candidate = next(c for c in candidates if c["candidate"] == evidence["candidate"])
        if candidate.get("passed") is not True or not all(candidate.get("checks", {}).values()):
            raise ValueError("candidate failed development validation")
        if selection["selection"].get(role) != evidence["candidate"]:
            raise ValueError("candidate not frozen before holdout")
        raw = next(m for m in registry["models"] if m["model_id"] == mid_id)
        if digest(raw) != evidence["model_sha256"]:
            raise ValueError("coefficients changed")
        models[mid_id] = LinearModel.from_dict(raw)
        roles[mid_id] = role
    return models, roles


def native_fetcher(ib: Any) -> Any:
    from app.fx.ibkr.adapter import forex_contract
    from app.fx.research.history import bars_to_frame

    async def fetch(symbol: str, timeframe: str) -> pd.DataFrame:
        if timeframe != "M5":
            raise ValueError("M5 only")
        c = forex_contract(symbol)
        sides = {}
        for side in ("BID", "ASK"):
            rows = await ib.reqHistoricalDataAsync(
                c, "", "4 D", "5 mins", side, False, formatDate=2
            )
            df = bars_to_frame(rows).set_index("ts").sort_index()
            sides[side] = df.loc[~df.index.duplicated()].add_prefix(side.lower() + "_")
        return sides["BID"].join(sides["ASK"], how="inner")

    return fetch


def protocol_from_hash(value: str) -> Any:
    from app.fx.research.m5_aula_validation import PROTOCOL_AULA

    for p in (PROTOCOL, PROTOCOL_AULA):
        if digest(p.to_dict()) == value:
            return p
    raise ValueError("unsupported pipeline protocol")
