"""Operations status for the dashboard (read-only; never cached by the UI)."""

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.alerts.monitor import paper_account_dirs, read_paper_account
from app.data.calendar import TradingCalendar
from app.operations import sessions as cal_
from app.operations.config import OperationsConfig
from app.operations.lock import PipelineLock
from app.operations.scheduler import scheduler_status
from app.operations.store import OpsStore


def overview(
    ops_root: Path,
    paper_root: Path,
    now: datetime,
    latest_stored: str | None,
    config: OperationsConfig | None = None,
) -> dict[str, Any]:
    cfg = config or OperationsConfig()
    cal = TradingCalendar(cfg.exchange)
    store = OpsStore.read_only(ops_root)
    paper_last = []
    for p in paper_account_dirs(paper_root):
        acc = read_paper_account(p)
        snaps = [e for e in acc.events if e["event_type"] == "snapshot"]
        if snaps:
            paper_last.append(pd.Timestamp(snaps[-1]["session"]).tz_convert(cal.tz).date())
    hist = store.history(50) if store else []
    alert_stage = next(
        (s for r in hist for s in reversed(r["stages"]) if s["stage"] == "ALERTS"), None
    )
    eligible = cal_.latest_eligible_session(cal, now, cfg.grace_minutes)
    return {
        "latest_completed_session": None if eligible is None else eligible.isoformat(),
        "next_session_eligible_at": cal_.next_eligibility(cal, now, cfg.grace_minutes).isoformat(),
        "latest_ingested_session": latest_stored,
        "latest_paper_session": max(paper_last).isoformat() if paper_last else None,
        "paper_accounts": len(paper_last),
        "latest_alert_evaluation": None
        if alert_stage is None
        else f"session {alert_stage['session']} at {alert_stage['finished_at']}",
        "checkpoint": None if store is None else store.state["last_completed_session"],
        "last_run": None if store is None else store.state["last_run"],
        "lock": PipelineLock(ops_root).owner(),
        "scheduler": scheduler_status(store, now, cfg.scheduler_poll_seconds),
        "metrics": None if store is None else store.metrics(),
        "grace_minutes": cfg.grace_minutes,
    }


def history_table(ops_root: Path, limit: int = 20) -> pd.DataFrame:
    store = OpsStore.read_only(ops_root)
    rows = [
        {
            "run_id": r["run_id"],
            "status": r["status"],
            "mode": r.get("mode"),
            "started_at": r.get("started_at"),
            "completed_at": r.get("completed_at"),
            "sessions": ", ".join(f"{k}:{v}" for k, v in r["sessions"].items()),
            "errors": "; ".join(r.get("errors") or []),
        }
        for r in (store.history(limit) if store else [])
    ]
    return pd.DataFrame(
        rows,
        columns=["run_id", "status", "mode", "started_at", "completed_at", "sessions", "errors"],
    )


def stage_table(run: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for s in run.get("stages", []):
        secs = (
            datetime.fromisoformat(s["finished_at"]) - datetime.fromisoformat(s["started_at"])
        ).total_seconds()
        rows.append(
            {
                "stage": s["stage"],
                "session": s["session"],
                "status": s["status"],
                "seconds": round(secs, 2),
                "error": s["error"],
            }
        )
    return pd.DataFrame(rows, columns=["stage", "session", "status", "seconds", "error"])


def last_run_detail(ops_root: Path) -> dict[str, Any] | None:
    store = OpsStore.read_only(ops_root)
    hist = store.history(1) if store else []
    return hist[0] if hist else None
