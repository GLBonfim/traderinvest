"""Lightweight monitoring: component health and read-only collection of the inputs the alert
rules need (database rows, paper ledgers). No metrics server; plain structured dicts.

Paper accounts are read WITHOUT `PaperStore` (which may complete an interrupted commit): the
state file and the ledger are parsed and verified read-only, and only events up to the
committed pointer are used.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.safety import SAFE_TRADING_MODES
from app.database.models import DataQualityEvent, IngestionRun
from app.paper.config import PaperConfig
from app.paper.ledger import LedgerError, verify_chain
from app.paper.state import StateError, loads
from app.risk.config import SCENARIOS

OK, DEGRADED, FAILED = "ok", "warning", "critical"


@dataclass(frozen=True)
class PaperAccountRead:
    account_id: str
    strategy_id: str
    instrument: str
    initial_capital: float
    events: list[dict[str, Any]]  # committed, verified
    error_kind: str | None  # PAPER_STATE_CORRUPT | PAPER_LEDGER_VERIFICATION_FAILED | ...
    error: str


def read_paper_account(path: Path) -> PaperAccountRead:
    """Read-only, verified view of one account directory (never modifies anything)."""
    acct = path.name
    try:
        meta = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        ((sid, _),) = meta["strategy_versions"].items()
        instrument = str(meta["instrument"])
        scen = next((s for s in SCENARIOS if s.name == meta["risk_scenario"]), None)
        if scen is None:
            raise ValueError(f"unknown risk scenario {meta['risk_scenario']!r}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return PaperAccountRead(
            acct,
            "?",
            "?",
            0.0,
            [],
            "PAPER_ACCOUNT_ERROR",
            f"manifest unreadable: {type(exc).__name__}: {exc}",
        )
    cfg = PaperConfig(risk=scen)
    try:
        _, pointer = loads((path / "state.json").read_text(encoding="utf-8"), cfg)
    except (OSError, StateError) as exc:
        return PaperAccountRead(
            acct, sid, instrument, 0.0, [], "PAPER_STATE_CORRUPT", f"{type(exc).__name__}: {exc}"
        )
    try:
        lines = (path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        verify_chain(events, acct)
        seq = int(pointer["seq"])
        if len(events) < seq or (seq and events[seq - 1]["hash"] != pointer["last_hash"]):
            raise LedgerError("ledger does not match the committed state pointer")
    except (OSError, ValueError, LedgerError) as exc:
        return PaperAccountRead(
            acct,
            sid,
            instrument,
            0.0,
            [],
            "PAPER_LEDGER_VERIFICATION_FAILED",
            f"{type(exc).__name__}: {exc}",
        )
    return PaperAccountRead(
        acct, sid, instrument, float(cfg.backtest.initial_capital), events[:seq], None, ""
    )


def paper_account_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists())


def ingestion_runs_since(session: Session, instrument_id: int, since: date) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.instrument_id == instrument_id)
        .order_by(IngestionRun.id)
    ).all()
    return [
        {
            "run_id": r.id,
            "provider": r.provider,
            "status": r.status,
            "error": r.error,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "requested_start": r.requested_start,
            "requested_end": r.requested_end,
        }
        for r in rows
        if r.started_at.date() >= since
    ]


def quality_events_since(session: Session, since: date) -> list[dict[str, Any]]:
    rows = session.scalars(select(DataQualityEvent).order_by(DataQualityEvent.id)).all()
    return [
        {
            "id": e.id,
            "check_name": e.check_name,
            "severity": e.severity,
            "description": e.description,
            "action_taken": e.action_taken,
            "detected_at": e.detected_at,
            "bar_ts": e.bar_ts,
            "ingestion_run_id": e.ingestion_run_id,
            "details": e.details,
        }
        for e in rows
        if e.detected_at.date() >= since
    ]


def trading_mode_ok(settings: Settings) -> bool:
    return settings.trading_mode in SAFE_TRADING_MODES


def summarize(components: dict[str, dict[str, Any]]) -> str:
    states = {c["status"] for c in components.values()}
    return FAILED if FAILED in states else DEGRADED if DEGRADED in states else OK


def component(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "detail": detail, **extra}


def now_iso(now: datetime) -> str:
    return now.isoformat()
