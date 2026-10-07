"""Audit ledger output: one directory per replay, git-ignored (data/paper/<id>/).

Files per strategy: decisions, orders, fills, portfolio, reconciliation (CSV). A manifest
(JSON) records versions, fingerprints, git commit (+dirty flag), configuration, counts.
"""

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.paper.config import MODE, PAPER_VERSION, PaperConfig
from app.paper.engine import PaperRun


def git_commit() -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 (read-only provenance)
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607 (read-only provenance)
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        return f"{head}+dirty" if dirty else head
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def manifest(runs: dict[str, PaperRun], config: PaperConfig) -> dict[str, Any]:
    return {
        "mode": MODE,
        "note": "Historical replay; not live trading; no real-money execution exists.",
        "paper_version": PAPER_VERSION,
        "git_commit": git_commit(),
        "config_fingerprint": config.fingerprint(),
        "risk_scenario": config.risk.name,
        "risk_fingerprint": config.risk.fingerprint(),
        "backtest_fingerprint": config.backtest.fingerprint(),
        "config": asdict(config),
        "strategies": {
            sid: {
                "run_id": r.run_id,
                "data_fingerprint": r.data_fingerprint,
                "decisions": len(r.decisions),
                "orders": len(r.orders),
                "fills": len(r.fills),
                "rejected_orders": int((r.orders["status"] == "REJECTED").sum()),
                "pending_order": r.pending_order is not None,
                "final_equity": float(r.portfolio["equity"].iloc[-1]),
            }
            for sid, r in runs.items()
        },
    }


def write_ledger(runs: dict[str, PaperRun], config: PaperConfig, out_dir: Path) -> Path:
    meta = manifest(runs, config)
    target = out_dir / f"paper-{meta['config_fingerprint']}"
    target.mkdir(parents=True, exist_ok=True)
    for sid, r in runs.items():
        for name in ("decisions", "orders", "fills", "portfolio", "reconciliation"):
            getattr(r, name).to_csv(target / f"{sid}__{name}.csv", index=False)
    (target / "manifest.json").write_text(json.dumps(meta, indent=2, default=str))
    return target
