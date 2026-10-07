"""Append-only, hash-chained paper ledger events; account reconstruction; replay reports.

Event (one JSON object per line in `ledger.jsonl`):
    seq         1, 2, 3, ... (contiguous)
    event_id    <account_id>-E<seq:08d>
    event_type  order | fill | reconciliation | decision | snapshot
    session     session (bar) timestamp the event belongs to
    account_id
    record      the typed record (models.py) as JSON (NaN -> null, timestamps ISO UTC)
    prev_hash   hash of the previous event ("" for the first)
    hash        sha256 of the canonical JSON of all fields above
Per session the order is: [order, fill?, reconciliation] (if an instruction was pending),
decision, snapshot. Every field is known when the session is processed, so the events through
T never depend on data after T. The chain makes any edit, deletion or reordering detectable.
"""

import hashlib
import json
import math
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from app import __version__
from app.paper.config import MODE, PAPER_VERSION, PaperConfig

if TYPE_CHECKING:
    from app.paper.engine import PaperRun
    from app.paper.trader import StepResult


class LedgerError(RuntimeError):
    """The ledger is corrupt or inconsistent with the stored state; nothing was modified."""


def _jsonable(v: Any) -> Any:
    if isinstance(v, pd.Timestamp):
        return v.tz_convert("UTC").isoformat()
    if isinstance(v, float):
        if math.isinf(v):
            raise LedgerError("infinite value in a ledger record")
        return None if math.isnan(v) else v
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def record_json(record: Any) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in asdict(record).items()}


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def event_hash(event: dict[str, Any]) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def step_events(
    step: "StepResult", account_id: str, start_seq: int, prev_hash: str
) -> list[dict[str, Any]]:
    """Ledger events of one processed session, chained after (start_seq - 1, prev_hash)."""
    records: list[tuple[str, Any]] = []
    if step.order is not None:
        records.append(("order", step.order))
    if step.fill is not None:
        records.append(("fill", step.fill))
    if step.reconciliation is not None:
        records.append(("reconciliation", step.reconciliation))
    records += [("decision", step.decision), ("snapshot", step.snapshot)]
    out, seq, prev = [], start_seq, prev_hash
    session = _jsonable(step.session)
    for etype, rec in records:
        ev: dict[str, Any] = {
            "seq": seq,
            "event_id": f"{account_id}-E{seq:08d}",
            "event_type": etype,
            "session": session,
            "account_id": account_id,
            "record": record_json(rec),
            "prev_hash": prev,
        }
        ev["hash"] = event_hash(ev)
        out.append(ev)
        prev, seq = ev["hash"], seq + 1
    return out


def verify_chain(events: list[dict[str, Any]], account_id: str) -> None:
    prev = ""
    for i, ev in enumerate(events, start=1):
        if ev.get("seq") != i or ev.get("account_id") != account_id:
            raise LedgerError(f"ledger event {i}: wrong sequence or account")
        if ev.get("prev_hash") != prev or ev.get("hash") != event_hash(ev):
            raise LedgerError(f"ledger event {i}: hash chain broken (edited or reordered)")
        if ev.get("event_id") != f"{account_id}-E{i:08d}":
            raise LedgerError(f"ledger event {i}: unexpected event_id")
        prev = ev["hash"]


def reconstruct_snapshots(
    events: list[dict[str, Any]], initial_capital: float
) -> list[dict[str, float]]:
    """Rebuilds the account from fills only (same arithmetic as the broker) and values it at
    every snapshot's mark price. Returns one dict per snapshot event."""
    cash, shares = initial_capital, 0.0
    b_notional = b_costs = realized = costs_cum = 0.0
    out = []
    for ev in events:
        rec = ev["record"]
        if ev["event_type"] == "fill":
            notional, price, total = rec["notional"], rec["price"], rec["total_cost"]
            if rec["side"] == "buy":
                cash = cash - (notional + total)
                shares = shares + notional / price
                b_notional += notional
                b_costs += total
            else:
                cash = cash + (notional - total)
                before = shares
                shares = 0.0 if rec["action"] == "exit" else shares - notional / price
                if rec["action"] == "exit":
                    sold_n, sold_c = b_notional, b_costs
                    b_notional = b_costs = 0.0
                else:
                    frac = (before - shares) / before
                    sold_n, sold_c = b_notional * frac, b_costs * frac
                    b_notional -= sold_n
                    b_costs -= sold_c
                realized += (notional - sold_n) - (sold_c + total)
            costs_cum += total
        elif ev["event_type"] == "snapshot":
            mark = rec["mark_price"]
            holding = shares > 1e-12
            basis = b_notional + b_costs if holding else 0.0
            unrealized = shares * mark - basis if holding else 0.0
            out.append(
                {
                    "cash": cash,
                    "position_quantity": shares,
                    "equity": cash + shares * mark,
                    "cost_basis": basis,
                    "realized_pnl": realized,
                    "unrealized_pnl": unrealized,
                    "total_pnl": realized + unrealized,
                    "cumulative_costs": costs_cum,
                }
            )
    return out


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


def provenance(
    config: PaperConfig,
    *,
    mode: str,
    strategy_versions: dict[str, str],
    instrument: str,
    timeframe: str,
) -> dict[str, Any]:
    """Who/what/when of a paper account or replay. Never part of any record identity."""
    return {
        "mode": mode,
        "note": "Local simulation; does not transmit orders to any brokerage or market.",
        "app_version": __version__,
        "paper_version": PAPER_VERSION,
        "git_commit": git_commit(),
        "created_at": datetime.now(UTC).isoformat(),
        "instrument": instrument,
        "timeframe": timeframe,
        "strategy_versions": strategy_versions,
        "initial_capital": float(config.backtest.initial_capital),
        "config_fingerprint": config.fingerprint(),
        "risk_scenario": config.risk.name,
        "risk_fingerprint": config.risk.fingerprint(),
        "backtest_fingerprint": config.backtest.fingerprint(),
        "config": asdict(config),
    }


def manifest(runs: dict[str, "PaperRun"], config: PaperConfig) -> dict[str, Any]:
    first = next(iter(runs.values()), None)
    return {
        **provenance(
            config,
            mode=f"{MODE}:historical_replay",
            strategy_versions={sid: r.strategy_version for sid, r in runs.items()},
            instrument=first.instrument if first else "",
            timeframe=first.timeframe if first else "",
        ),
        # metadata only: identifies the input dataset of this report, never a record
        "data_fingerprint": first.data_fingerprint if first else "",
        "strategies": {
            sid: {
                "account_id": r.account_id,
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


def write_ledger(runs: dict[str, "PaperRun"], config: PaperConfig, out_dir: Path) -> Path:
    """Replay report: CSV per record type and strategy + manifest (git-ignored data/paper/)."""
    meta = manifest(runs, config)
    target = out_dir / f"replay-{meta['config_fingerprint']}"
    target.mkdir(parents=True, exist_ok=True)
    for sid, r in runs.items():
        for name in ("decisions", "orders", "fills", "portfolio", "reconciliation", "trades"):
            getattr(r, name).to_csv(target / f"{sid}__{name}.csv", index=False)
    (target / "manifest.json").write_text(json.dumps(meta, indent=2, default=str))
    return target
