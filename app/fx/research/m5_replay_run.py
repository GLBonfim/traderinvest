"""Accelerated, isolated M5 system replay. Never broker paper execution or qualification."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.agents.signals import features
from app.fx.agents.trained import FEATURE_NAMES, feature_frame
from app.fx.config import FxConfig, RiskSettings, SimulationSettings
from app.fx.proposals import AgentRole
from app.fx.research import m5_boosted_training as boosted
from app.fx.research import m5_economic_training as economic
from app.fx.research import m5_lesson_training as lesson
from app.fx.research.m5_validation import analyst_frame, mid_of, pattern_frame, write_json
from app.fx.research.replay import run_replay

ROLES = {
    "reversao": AgentRole.MEAN_REVERSION,
    "continuacao": AgentRole.TREND,
    "impulso": AgentRole.MOMENTUM,
}


def frozen_model(root: Path, candidate: str, fold: str, start: str) -> dict[str, Any]:
    candidates = json.loads((root / "candidates.json").read_text(encoding="utf-8"))
    if isinstance(candidates, dict):
        candidates = candidates["candidates"]
    selected = next(c for c in candidates if c["candidate"] == candidate)
    evidence = next(f for f in selected["folds"] if f["fold"] == fold)
    model = json.loads(
        (root / "models" / f"{candidate}-fold-{fold}.json").read_text(encoding="utf-8")
    )
    if not isinstance(model, dict):
        raise ValueError("model object required")
    actual = hashlib.sha256(
        json.dumps(model, sort_keys=True, allow_nan=False, default=str).encode()
    ).hexdigest()
    if actual != evidence["model_sha256"]:
        raise ValueError("frozen model hash differs")
    if pd.Timestamp(model["training_end"]) > pd.Timestamp(start):
        raise ValueError("model training overlaps replay window")
    if model.get("offline_only") is not True or model.get("execution_enabled") is not False:
        raise ValueError("offline research artifact required")
    return model


def verify_replay_chunks(
    data: Path, manifest: dict[str, Any], start: str, end: str
) -> list[dict[str, Any]]:
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    verified = []
    for chunk in manifest["chunks"]:
        if pd.Timestamp(chunk["first"]) >= hi or pd.Timestamp(chunk["last"]) < lo:
            continue
        path = data / chunk["pair"] / Path(str(chunk["file"]).replace("\\", "/")).name
        if hashlib.sha256(path.read_bytes()).hexdigest() != chunk["sha256"]:
            raise ValueError("replay chunk hash differs from frozen source manifest")
        verified.append(chunk)
    if not verified:
        raise ValueError("no verified development chunks in replay window")
    return verified


def load_replay_frame(
    data: Path, symbol: str, chunks: list[dict[str, Any]], start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    from app.fx.research.history import with_spread_floor
    from app.fx.research.m5_history import validate

    sides = {}
    for side in ("BID", "ASK"):
        parts = []
        for chunk in chunks:
            if chunk["pair"] != symbol or chunk["side"] != side:
                continue
            path = data / symbol / Path(str(chunk["file"]).replace("\\", "/")).name
            frame = pd.read_parquet(
                path,
                engine="pyarrow",
                filters=[("ts", ">=", start.to_pydatetime()), ("ts", "<", end.to_pydatetime())],
            )
            if not frame.empty:
                parts.append(frame)
        if not parts:
            raise ValueError("verified replay data absent for pair/side")
        frame = validate(pd.concat(parts, ignore_index=True)).set_index("ts").sort_index()
        frame = frame.loc[(frame.index >= start) & (frame.index < end)]
        sides[side] = frame[["open", "high", "low", "close"]].add_prefix(side.lower() + "_")
    return with_spread_floor(sides["BID"].join(sides["ASK"], how="inner"), symbol)


def select_models(source: Path, depth: int, start: str) -> dict[str, Any]:
    if depth not in (3, 5):
        raise ValueError("frozen candidate depth must be3 or5")
    return {role: frozen_model(source, f"{role}-{depth}", "2025", start) for role in ROLES}


def run(data: Path, source: Path, out: Path, start: str, end: str, depth: int = 3) -> None:
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    if lo.tz is None or hi.tz is None or not lo < hi <= pd.Timestamp("2026-01-01T00:00:00Z"):
        raise ValueError("ordered UTC development window ending before2026 required")
    models = select_models(source, depth, lo.isoformat())
    if any(tuple(model["feature_names"]) != economic.NAMES for model in models.values()):
        raise ValueError("frozen ordered51-features required")
    manifest_path = source / "data_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=False)
    provider_paths = [
        Path(__file__),
        Path(lesson.__file__),
        Path(economic.__file__),
        Path(boosted.__file__),
        Path(__file__).with_name("replay.py"),
        Path(__file__).parents[1] / "config.py",
        Path(__file__).parents[1] / "risk.py",
        Path(__file__).parents[1] / "orchestrator.py",
        Path(__file__).parents[1] / "sim/broker.py",
        Path(__file__).parents[1] / "accounting.py",
        Path(__file__).parents[1] / "agents/trained.py",
        Path(__file__).parents[1] / "agents/signals.py",
        Path(__file__).with_name("m5_validation.py"),
        Path(__file__).parents[2] / "candles/lesson_m5.py",
    ]
    protocol = {
        "schema": "m5-system-replay/1",
        "created_at": datetime.now(UTC).isoformat(),
        "start": lo.isoformat(),
        "end": hi.isoformat(),
        "depth": depth,
        "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "fold": "2025",
        "model_source": str(source.resolve()),
        "model_sha256": {
            role: hashlib.sha256(
                json.dumps(m, sort_keys=True, allow_nan=False, default=str).encode()
            ).hexdigest()
            for role, m in models.items()
        },
        "provider_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in provider_paths
        },
        "research_simulation_only": True,
        "statistically_approved": False,
        "broker_orders_enabled": False,
        "feature_warmup": "14 calendar days",
        "limitations": [
            "OHLC subpath is simulated, not tick history",
            "Features use bounded replay warmup, not live parity proof",
            "Failed models admitted only to isolated SIMULATION test",
            "No automatic candidate qualification or registry updates",
        ],
    }
    write_json(out / "protocol.json", protocol)
    chunks = verify_replay_chunks(
        data, manifest, (lo - pd.Timedelta(days=14)).isoformat(), hi.isoformat()
    )
    write_json(out / "replay-data-chunks.json", chunks)
    frozen_dir = out / "sources"
    frozen_dir.mkdir()
    for number, path in enumerate(provider_paths):
        (frozen_dir / f"{number}-{path.name}").write_bytes(path.read_bytes())
    proposal_counts = {}
    frames, metadata, regimes = {}, {}, {}
    signals: dict[AgentRole, dict[str, pd.DataFrame]] = {role: {} for role in ROLES.values()}
    for symbol in ("EURUSD", "EURGBP", "EURJPY"):
        bars = load_replay_frame(data, symbol, chunks, lo - pd.Timedelta(days=14), hi)
        mid = mid_of(bars)
        f = feature_frame(mid, "M5")
        patterns = pattern_frame(mid)
        context = lesson.lesson_features(mid, patterns)
        x = pd.concat([f[list(FEATURE_NAMES)], context], axis=1).to_numpy(float)
        x = economic.augment(bars, f, symbol, x)
        a = analyst_frame(f)
        frames[symbol] = bars
        metadata[symbol] = features(mid, bars.ask_close - bars.bid_close)
        regimes[symbol] = pd.DataFrame(
            {"regime": np.where(a.trend < 0, "bearish", "other"), "veto": ~a.tradable.astype(bool)},
            index=bars.index,
        )
        for role, enum in ROLES.items():
            forecast = boosted.portable_predict(models[role], np.nan_to_num(x))
            mask = lesson.research_mask(f, patterns, context, role)
            signals[enum][symbol] = lesson.signals(bars, f, mask, forecast, role, symbol)
        proposal_counts[symbol] = {
            role.value: {
                "research_signals": int(((sig["dir"] != 0) & (sig.index >= lo)).sum()),
                "score_at_least_risk_min": int(
                    ((sig["dir"] != 0) & (sig["score"] >= 0.55) & (sig.index >= lo)).sum()
                ),
            }
            for role, by_pair in signals.items()
            for sig in [by_pair[symbol]]
        }
        print(f"Prepared nativeM5 {symbol}: {len(bars)} bars", flush=True)
    config = FxConfig(
        timeframe="M5",
        pairs=list(frames),
        simulation=SimulationSettings(
            commission_per_million=40.0, slippage_pips=0.0, warm_up_minutes=0
        ),
        risk=RiskSettings(
            risk_per_trade_pct=0.1,
            daily_loss_limit_pct=1.0,
            max_drawdown_pct=5.0,
            max_entries_per_day=5,
            max_open_positions=3,
        ),
    )
    report = run_replay(
        frames,
        lo.to_pydatetime(),
        hi.to_pydatetime(),
        signals,
        {role: 96 for role in ROLES.values()},
        metadata,
        regimes,
        frozenset(role.value for role in ROLES.values()),
        work_dir=out,
        timeframe="M5",
        config_override=config,
        max_order_units=5000,
        commission_min_usd=2.0,
    )
    report["daily_close_max_drawdown_pct"] = report.pop("max_drawdown_pct")
    report["simulation_admitted_roles"] = report.pop("approved", [])
    report["proposal_counts"] = proposal_counts
    report.update(
        protocol=protocol,
        qualified=False,
        broker_orders_enabled=False,
        research_simulation_only=True,
    )
    write_json(out / "summary.json", report)
    print(json.dumps(report, default=str), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    parser.add_argument(
        "--source", type=Path, default=Path("data/fx/training/run-m5-balanced-gapfix-v1-20261028")
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", default="2025-01-01T00:00:00+00:00")
    parser.add_argument("--end", default="2025-02-01T00:00:00+00:00")
    parser.add_argument("--depth", type=int, choices=(3, 5), default=3)
    args = parser.parse_args()
    run(args.data, args.source, args.out, args.start, args.end, args.depth)
