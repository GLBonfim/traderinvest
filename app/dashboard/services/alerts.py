"""Alerts & monitoring views for the dashboard (read-only; never cached by the UI).

The only write the dashboard can trigger here is an explicit "evaluate alerts now" run, which
writes to the alert store only (`app.alerts.engine.run_alerts`) and never touches research or
paper-trading state.
"""

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from app import __version__
from app.alerts.engine import DEFAULT_ALERTS_ROOT, verify_store
from app.alerts.monitor import DEGRADED, FAILED, OK, paper_account_dirs, read_paper_account
from app.alerts.store import AlertStore, AlertStoreError
from app.dashboard.services import health, market

ALERT_COLUMNS = (
    "occurred_at",
    "severity",
    "source",
    "event_type",
    "instrument",
    "strategy_id",
    "account_id",
    "title",
    "alert_id",
    "recorded_at",
)


def load_alerts(root: Path = DEFAULT_ALERTS_ROOT) -> tuple[pd.DataFrame, dict[str, Any] | None]:
    """(alerts newest first with delivery status, store metrics) or an empty frame if no store."""
    try:
        store = AlertStore(root, create=False)
    except AlertStoreError:
        return pd.DataFrame(columns=[*ALERT_COLUMNS, "delivery"]), None
    alerts = pd.DataFrame(store.alerts())
    if alerts.empty:
        return pd.DataFrame(columns=[*ALERT_COLUMNS, "delivery"]), store.metrics()
    status = store.delivery_status()

    def delivery(aid: str) -> str:
        parts = []
        for (a, ch), s in sorted(status.items()):
            if a == aid:
                state = (
                    "delivered"
                    if s["delivered"]
                    else "failed (final)"
                    if s["permanent_failure"]
                    else f"retrying ({s['attempts']})"
                )
                parts.append(f"{ch}: {state}")
        return ", ".join(parts) or "stored (dashboard)"

    alerts["delivery"] = alerts["alert_id"].map(delivery)
    return alerts.iloc[::-1].reset_index(drop=True), store.metrics()


def filter_alerts(
    alerts: pd.DataFrame,
    *,
    severities: list[str] | None = None,
    sources: list[str] | None = None,
    strategy: str | None = None,
    account: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    out = alerts
    if severities:
        out = out[out["severity"].isin(severities)]
    if sources:
        out = out[out["source"].isin(sources)]
    if strategy:
        out = out[out["strategy_id"] == strategy]
    if account:
        out = out[out["account_id"] == account]
    if start or end:
        day = pd.to_datetime(out["occurred_at"], utc=True).dt.tz_convert("America/New_York").dt.date
        if start:
            out = out[day >= start]
        if end:
            out = out[day <= end]
    return out.reset_index(drop=True)


def alert_detail(alerts: pd.DataFrame, alert_id: str) -> dict[str, Any]:
    row = alerts[alerts["alert_id"] == alert_id]
    if row.empty:
        raise KeyError(alert_id)
    return {str(k): v for k, v in row.iloc[0].to_dict().items()}


def monitoring_state(
    engine: Engine,
    paper_root: Path,
    alerts_root: Path = DEFAULT_ALERTS_ROOT,
    ident: market.DatasetIdentity | None = None,
    fresh: market.Freshness | None = None,
    last_ingestion: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Component health from read-only checks (database, data freshness, latest ingestion,
    paper accounts and ledger integrity, alert store, application version)."""
    out: dict[str, dict[str, Any]] = {}
    db = health.database_status(engine)
    out["database"] = {
        "status": OK if db["status"] == "ok" else FAILED,
        "detail": db["error"] or "reachable",
    }
    if fresh is not None:
        out["market_data"] = {
            "status": DEGRADED if fresh.stale else OK,
            "detail": f"last session {fresh.last_bar_session}; expected "
            f"{fresh.expected_session}; {fresh.missing_sessions} missing",
        }
    if last_ingestion is not None:
        out["ingestion"] = {
            "status": OK if last_ingestion["status"] == "succeeded" else DEGRADED,
            "detail": f"run {last_ingestion['run_id']} {last_ingestion['status']}"
            f" at {last_ingestion['finished_at']}",
        }
    accounts = [read_paper_account(p) for p in paper_account_dirs(paper_root)]
    bad = [a for a in accounts if a.error_kind]
    out["paper_ledgers"] = {
        "status": FAILED if bad else OK,
        "detail": f"{len(accounts)} account(s); "
        + ("; ".join(f"{a.account_id}: {a.error_kind}" for a in bad) or "all hash chains verified"),
    }
    v = verify_store(alerts_root)
    out["alert_store"] = {
        "status": OK
        if v["status"] == OK
        else (DEGRADED if "no alert store" in str(v.get("error", "")) else FAILED),
        "detail": v.get("error")
        or f"{v['alerts']} alerts, {v['deliveries']} delivery records; hash chains verified",
    }
    out["application"] = {"status": OK, "detail": f"version {__version__}"}
    return out
