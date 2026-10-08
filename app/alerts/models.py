"""Alert records. An alert REPORTS an existing domain event; it never creates a decision.

Identity (point-in-time, Phase 12 discipline): `dedup_key` is built only from information
available when the underlying event happened —
    event_type | instrument | strategy or account | session (or episode) | relevant state
and `alert_id = sha256(dedup_key)[:16]`. No dataset fingerprint, application version or
wall-clock time enters the identity, so appending bars, re-running or upgrading never renames
a historical alert.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any

INFO, WARNING, CRITICAL = "INFO", "WARNING", "CRITICAL"
SEVERITIES = (INFO, WARNING, CRITICAL)
SEVERITY_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}
PAPER_TAG = "PAPER SIMULATION"

# Sources (the component that produced the underlying event)
SRC_DATA = "MarketData"
SRC_REGIME = "MarketRegimeEngine"
SRC_STRATEGY = "StrategyEngine"
SRC_RISK = "RiskManager"
SRC_PAPER = "PaperTrading"
SRC_SYSTEM = "System"
SRC_SAFETY = "Safety"
SRC_OPS = "Operations"

# event_type -> (severity, source). Severity describes operational attention, never a
# financial opportunity.
EVENT_TYPES: dict[str, tuple[str, str]] = {
    # market / data
    "DATA_STALE": (WARNING, SRC_DATA),
    "DATA_MISSING_SESSION": (WARNING, SRC_DATA),
    "DATA_INGESTION_FAILED": (WARNING, SRC_DATA),
    "DATA_PROVIDER_FAILURE": (CRITICAL, SRC_DATA),
    "DATA_QUALITY_EVENT": (INFO, SRC_DATA),  # severity mapped from the stored event
    # market regime
    "REGIME_TREND_CHANGED": (INFO, SRC_REGIME),
    "REGIME_VOLATILITY_CHANGED": (INFO, SRC_REGIME),
    "REGIME_MOMENTUM_CHANGED": (INFO, SRC_REGIME),
    "REGIME_COMPOSITE_CHANGED": (INFO, SRC_REGIME),
    "REGIME_VOLATILITY_EXTREME": (WARNING, SRC_REGIME),
    # strategy
    "STRATEGY_STATE_CHANGED": (INFO, SRC_STRATEGY),
    # risk (paper accounts)
    "RISK_EXPOSURE_REDUCED": (WARNING, SRC_RISK),
    "RISK_EXPOSURE_BLOCKED": (WARNING, SRC_RISK),
    "RISK_DRAWDOWN_WARNING": (WARNING, SRC_RISK),
    "RISK_LOCK_COOLDOWN_STARTED": (WARNING, SRC_RISK),
    "RISK_LOCK_COOLDOWN_ENDED": (INFO, SRC_RISK),
    "RISK_STOP_TRIGGERED": (WARNING, SRC_RISK),
    # paper trading
    "PAPER_ENTRY_SCHEDULED": (INFO, SRC_PAPER),
    "PAPER_EXIT_SCHEDULED": (INFO, SRC_PAPER),
    "PAPER_REBALANCE_SCHEDULED": (INFO, SRC_PAPER),
    "PAPER_FILL_EXECUTED": (INFO, SRC_PAPER),
    "PAPER_ORDER_REJECTED": (WARNING, SRC_PAPER),
    "PAPER_ACCOUNT_ERROR": (CRITICAL, SRC_PAPER),
    "PAPER_STATE_CORRUPT": (CRITICAL, SRC_PAPER),
    "PAPER_LEDGER_VERIFICATION_FAILED": (CRITICAL, SRC_PAPER),
    # system / safety
    "SYSTEM_DATABASE_UNAVAILABLE": (CRITICAL, SRC_SYSTEM),
    "SYSTEM_DASHBOARD_RESULTS_STALE": (INFO, SRC_SYSTEM),
    "SYSTEM_UNSUPPORTED_TRADING_MODE": (CRITICAL, SRC_SAFETY),
    "SAFETY_NEGATIVE_CASH": (CRITICAL, SRC_SAFETY),
    "SAFETY_EXPOSURE_ABOVE_LIMIT": (CRITICAL, SRC_SAFETY),
    "SAFETY_NEGATIVE_POSITION": (CRITICAL, SRC_SAFETY),
    "SAFETY_ACCOUNTING_RECONCILIATION_FAILED": (CRITICAL, SRC_SAFETY),
    # operations (Phase 14.5): pipeline orchestration events
    "OPS_STAGE_FAILED": (WARNING, SRC_OPS),
    "OPS_SESSION_DATA_MISSING": (WARNING, SRC_OPS),
    "OPS_CATCH_UP_REQUIRED": (INFO, SRC_OPS),
    "OPS_LOCK_CONFLICT": (WARNING, SRC_OPS),
    "OPS_REPEATED_FAILURE": (CRITICAL, SRC_OPS),
    # channel self-test (never a market or trading event)
    "TEST_NOTIFICATION": (INFO, SRC_SYSTEM),
}


def alert_id_for(dedup_key: str) -> str:
    return hashlib.sha256(dedup_key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class AlertEvent:
    alert_id: str
    dedup_key: str
    event_type: str
    severity: str
    source: str
    occurred_at: str  # ISO UTC: when the underlying event happened (bar close, fill, detection)
    session: str | None  # session date (YYYY-MM-DD) the event belongs to, if any
    instrument: str | None
    strategy_id: str | None
    account_id: str | None
    title: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)  # structured reason (real values)
    rule_version: str = "1.0.0"
    app_version: str = ""  # provenance only; never part of the identity


def make_alert(
    event_type: str,
    *,
    key_parts: tuple[str | None, ...],
    occurred_at: str,
    title: str,
    message: str,
    payload: dict[str, Any],
    session: str | None = None,
    instrument: str | None = None,
    strategy_id: str | None = None,
    account_id: str | None = None,
    severity: str | None = None,
    app_version: str = "",
) -> AlertEvent:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown alert type {event_type!r}")
    default_sev, source = EVENT_TYPES[event_type]
    sev = severity or default_sev
    if sev not in SEVERITIES:
        raise ValueError(f"unknown severity {sev!r}")
    key = "|".join([event_type, *("" if p is None else str(p) for p in key_parts)])
    return AlertEvent(
        alert_id=alert_id_for(key),
        dedup_key=key,
        event_type=event_type,
        severity=sev,
        source=source,
        occurred_at=occurred_at,
        session=session,
        instrument=instrument,
        strategy_id=strategy_id,
        account_id=account_id,
        title=title,
        message=message,
        payload=payload,
        app_version=app_version,
    )
