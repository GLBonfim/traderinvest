"""Deterministic alert rules: pure functions of existing structured outputs.

Every rule is an explicit condition on recorded values (no model, no free-form judgement):
    regime        label(T) != label(T-1)                       -> REGIME_<DIM>_CHANGED
                  volatility(T) == extreme != volatility(T-1)  -> REGIME_VOLATILITY_EXTREME
    strategy      (state(T-1), state(T)) in TRANSITIONS          -> STRATEGY_STATE_CHANGED
    risk          intervention category changes into reduced/blocked; risk_state transitions
                  NORMAL->WARNING, ->LOCKED, LOCKED->other; "stop_triggered" in the reasons
    paper         decision pending_kind entry/exit/rebalance; fill; rejected order
    safety        snapshot invariants (cash >= 0, quantity >= 0, exposure <= 1,
                  total P&L == equity - initial within tolerance)
    data          calendar session missing inside the stored history; failed ingestion run;
                  stored data-quality events; provider failures
Rules fire on TRANSITIONS, never on a continuing state (no alert storms). An alert for session
T is determined by rows at or before T only; `since` only limits which sessions are reported.
"""

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.alerts.models import CRITICAL, INFO, PAPER_TAG, WARNING, AlertEvent, make_alert

NY = "America/New_York"
REGIME_DIMENSIONS = {
    "trend": ("trend_regime", ("trend_quality",)),
    "volatility": ("volatility_regime", ("volatility_measure", "volatility_percentile", "atr_pct")),
    "momentum": ("momentum_regime", ("momentum_rsi", "momentum_roc", "momentum_macd")),
    "composite": ("composite_regime", ("volatility_percentile", "trend_quality")),
}
STRATEGY_TRANSITIONS = {
    ("INSUFFICIENT_DATA", "LONG"),
    ("INSUFFICIENT_DATA", "FLAT"),
    ("FLAT", "LONG"),
    ("LONG", "FLAT"),
}
QUALITY_SEVERITY = {"info": INFO, "warning": WARNING, "error": CRITICAL, "critical": CRITICAL}


def iso(ts: Any) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").isoformat()


def session_of(ts: Any) -> str:
    return pd.Timestamp(ts).tz_convert(NY).date().isoformat()


def _num(v: Any) -> Any:
    if v is None or (isinstance(v, float | np.floating) and np.isnan(v)):
        return None
    return v.item() if isinstance(v, np.generic) else v


def _from(ts: pd.Timestamp, since: pd.Timestamp | None) -> bool:
    return since is None or ts >= since


# ── market regime ──


def regime_alerts(
    state: pd.DataFrame,
    observed_at: pd.Series,
    *,
    instrument: str,
    since: pd.Timestamp | None = None,
    app_version: str = "",
) -> list[AlertEvent]:
    """`state` = RegimeAnalysis.state (index = bar ts); `observed_at` = close time per bar."""
    out: list[AlertEvent] = []
    for dim, (col, measures) in REGIME_DIMENSIONS.items():
        labels = state[col]
        prev = labels.shift(1)
        changed = labels.ne(prev) & prev.notna()
        for ts in labels.index[changed.to_numpy()]:
            if not _from(ts, since):
                continue
            p, c = str(prev.loc[ts]), str(labels.loc[ts])
            row = state.loc[ts]
            payload = {
                "dimension": dim,
                "previous": p,
                "current": c,
                "age_sessions": _num(row.get(f"{dim}_age")),
                **{m: _num(row[m]) for m in measures},
            }
            common: dict[str, Any] = {
                "occurred_at": iso(observed_at.loc[ts]),
                "session": session_of(ts),
                "instrument": instrument,
                "app_version": app_version,
            }
            out.append(
                make_alert(
                    f"REGIME_{dim.upper()}_CHANGED",
                    key_parts=(instrument, session_of(ts), p, c),
                    title=f"{instrument} {dim} regime changed: {p} -> {c}",
                    message=f"Market regime ({dim}) changed from {p} to {c} at the close of "
                    f"{session_of(ts)}. Describes observed conditions; not a prediction or a "
                    "trade recommendation.",
                    payload=payload,
                    **common,
                )
            )
            if dim == "volatility" and c == "extreme":
                out.append(
                    make_alert(
                        "REGIME_VOLATILITY_EXTREME",
                        key_parts=(instrument, session_of(ts), p, c),
                        title=f"{instrument} volatility regime became extreme (from {p})",
                        message=f"Volatility regime is extreme at the close of {session_of(ts)} "
                        f"(volatility percentile {payload['volatility_percentile']}). Describes "
                        "observed conditions only.",
                        payload=payload,
                        **common,
                    )
                )
    return sorted(out, key=lambda a: (a.occurred_at, a.event_type))


# ── strategies ──


def strategy_alerts(
    signals: pd.DataFrame,
    *,
    instrument: str,
    since: pd.Timestamp | None = None,
    app_version: str = "",
) -> list[AlertEvent]:
    """`signals` = StrategyRun.signals (long format)."""
    out: list[AlertEvent] = []
    for sid, g in signals.groupby("strategy_id", sort=False):
        g = g.sort_values("bar_ts")
        prev = g["state"].shift(1)
        for i in np.flatnonzero((g["state"] != prev).to_numpy() & prev.notna().to_numpy()):
            r = g.iloc[i]
            p, c = str(prev.iloc[i]), str(r["state"])
            if (p, c) not in STRATEGY_TRANSITIONS or not _from(r["bar_ts"], since):
                continue
            out.append(
                make_alert(
                    "STRATEGY_STATE_CHANGED",
                    key_parts=(instrument, str(sid), session_of(r["bar_ts"]), p, c),
                    occurred_at=iso(r["observed_at"]),
                    session=session_of(r["bar_ts"]),
                    instrument=instrument,
                    strategy_id=str(sid),
                    app_version=app_version,
                    title=f"Research strategy {sid}: {p} -> {c}",
                    message=f"Research strategy state of {sid} changed from {p} to {c} at the "
                    f"close of {session_of(r['bar_ts'])} (effective "
                    f"{iso(r['effective_at'])}); reason: {r['reason']}. A research benchmark "
                    "state, not a recommendation.",
                    payload={
                        "previous": p,
                        "current": c,
                        "reason": r["reason"],
                        "strategy_version": r["strategy_version"],
                        "observed_at": iso(r["observed_at"]),
                        "effective_at": iso(r["effective_at"]),
                    },
                )
            )
    return sorted(out, key=lambda a: (a.occurred_at, a.strategy_id or ""))


# ── paper accounts (committed ledger events) ──


def _intervention(requested: float, approved: float) -> str:
    if requested > 0 and approved <= 1e-12:
        return "blocked"
    if approved < requested - 1e-12:
        return "reduced"
    return "none"


def paper_alerts(
    events: Sequence[dict[str, Any]],
    *,
    account_id: str,
    strategy_id: str,
    instrument: str,
    initial_capital: float,
    since: pd.Timestamp | None = None,
    app_version: str = "",
) -> list[AlertEvent]:
    """Alerts from the COMMITTED, hash-verified ledger of one paper account. The risk and
    lock state machines run over the whole ledger (point-in-time: each step only sees earlier
    events); only sessions >= `since` are reported."""
    out: list[AlertEvent] = []
    common: dict[str, Any] = {
        "instrument": instrument,
        "strategy_id": strategy_id,
        "account_id": account_id,
        "app_version": app_version,
    }
    last_cat, last_state = "none", "NORMAL"

    for ev in events:
        rec, etype = ev["record"], ev["event_type"]
        sess_ts = pd.Timestamp(ev["session"])
        report = _from(sess_ts, since)
        sess = session_of(sess_ts)
        if etype == "decision":
            req, app = float(rec["strategy_requested_target"]), float(rec["risk_approved_target"])
            kind = rec["pending_kind"]
            when = rec["observed_at"]
            base = {
                "decision_id": rec["decision_id"],
                "strategy_state": rec["strategy_state"],
                "requested_exposure": req,
                "approved_exposure": app,
                "risk_state": rec["risk_state"],
                "reasons": rec["intervention_reason"],
                "scheduled_execution_at": rec["scheduled_execution_at"],
            }
            if report and kind in ("entry", "exit", "rebalance"):
                out.append(
                    make_alert(
                        f"PAPER_{kind.upper()}_SCHEDULED",
                        key_parts=(account_id, sess, rec["decision_id"]),
                        occurred_at=when,
                        session=sess,
                        **common,
                        title=f"{PAPER_TAG}: {kind} scheduled for {strategy_id}",
                        message=f"{PAPER_TAG}. Risk-approved instruction created at the close of "
                        f"{sess}: {kind} to target exposure {app:.4f}, scheduled for "
                        f"{rec['scheduled_execution_at']}. Simulated; no order is sent anywhere.",
                        payload={**base, "pending_kind": kind},
                    )
                )
            cat = _intervention(req, app)
            if report and cat != last_cat and cat in ("reduced", "blocked"):
                t = "RISK_EXPOSURE_BLOCKED" if cat == "blocked" else "RISK_EXPOSURE_REDUCED"
                out.append(
                    make_alert(
                        t,
                        key_parts=(account_id, sess, f"{last_cat}->{cat}"),
                        occurred_at=when,
                        session=sess,
                        **common,
                        title=f"{PAPER_TAG}: risk layer {cat} requested exposure ({strategy_id})",
                        message=f"{PAPER_TAG}. RiskManager approved {app:.4f} of the requested "
                        f"{req:.4f} at the close of {sess}; reasons: "
                        f"{rec['intervention_reason'] or 'n/a'}.",
                        payload={**base, "previous_category": last_cat, "category": cat},
                    )
                )
            last_cat = cat
            state = rec["risk_state"]
            if report and state != last_state:
                lock_t: str | None = None
                if state == "WARNING" and last_state == "NORMAL":
                    lock_t = "RISK_DRAWDOWN_WARNING"
                elif state == "LOCKED":
                    lock_t = "RISK_LOCK_COOLDOWN_STARTED"
                elif last_state == "LOCKED":
                    lock_t = "RISK_LOCK_COOLDOWN_ENDED"
                if lock_t:
                    out.append(
                        make_alert(
                            lock_t,
                            key_parts=(account_id, sess, f"{last_state}->{state}"),
                            occurred_at=when,
                            session=sess,
                            **common,
                            title=f"{PAPER_TAG}: risk state {last_state} -> {state} "
                            f"({strategy_id})",
                            message=f"{PAPER_TAG}. Risk state changed from {last_state} to {state} "
                            f"at the close of {sess}.",
                            payload={**base, "previous_risk_state": last_state},
                        )
                    )
            last_state = state
            if report and "stop_triggered" in str(rec["intervention_reason"]).split(";"):
                out.append(
                    make_alert(
                        "RISK_STOP_TRIGGERED",
                        key_parts=(account_id, sess),
                        occurred_at=when,
                        session=sess,
                        **common,
                        title=f"{PAPER_TAG}: stop triggered on the close ({strategy_id})",
                        message=f"{PAPER_TAG}. Close-based stop triggered at the close of {sess}; "
                        "the exit is scheduled for the next session open.",
                        payload=base,
                    )
                )
        elif etype == "fill" and report:
            out.append(
                make_alert(
                    "PAPER_FILL_EXECUTED",
                    key_parts=(account_id, rec["fill_id"]),
                    occurred_at=rec["executed_at"],
                    session=sess,
                    **common,
                    title=f"{PAPER_TAG}: simulated {rec['action']} fill ({strategy_id})",
                    message=f"{PAPER_TAG}. Simulated {rec['side']} ({rec['action']}) of "
                    f"{abs(rec['quantity']):.6f} at the session open {rec['price']:.4f}; costs "
                    f"{rec['total_cost']:.2f}. Not a brokerage transaction.",
                    payload={
                        k: rec[k]
                        for k in (
                            "fill_id",
                            "order_id",
                            "side",
                            "action",
                            "price",
                            "quantity",
                            "notional",
                            "total_cost",
                            "realized_pnl",
                            "cash_after",
                            "position_after",
                        )
                    },
                )
            )
        elif etype == "order" and report and rec["status"] == "REJECTED":
            out.append(
                make_alert(
                    "PAPER_ORDER_REJECTED",
                    key_parts=(account_id, rec["order_id"]),
                    occurred_at=iso(sess_ts),
                    session=sess,
                    **common,
                    title=f"{PAPER_TAG}: simulated order rejected ({strategy_id})",
                    message=f"{PAPER_TAG}. The paper broker rejected order {rec['order_id']}: "
                    f"{rec['rejection_reason']}. Position unchanged.",
                    payload={
                        k: rec[k]
                        for k in ("order_id", "decision_id", "side", "action", "rejection_reason")
                    },
                )
            )
        elif etype == "snapshot" and report:
            out += _snapshot_safety(rec, sess, initial_capital, common)
    return out


def _snapshot_safety(
    rec: dict[str, Any], sess: str, initial: float, common: dict[str, Any]
) -> list[AlertEvent]:
    checks = (
        ("SAFETY_NEGATIVE_CASH", rec["cash"] < 0, "cash", rec["cash"]),
        (
            "SAFETY_NEGATIVE_POSITION",
            rec["position_quantity"] < 0,
            "position_quantity",
            rec["position_quantity"],
        ),
        (
            "SAFETY_EXPOSURE_ABOVE_LIMIT",
            rec["gross_exposure"] > 1 + 1e-12,
            "gross_exposure",
            rec["gross_exposure"],
        ),
        (
            "SAFETY_ACCOUNTING_RECONCILIATION_FAILED",
            abs(rec["total_pnl"] - (rec["equity"] - initial)) > 1e-6 * max(1.0, initial),
            "total_pnl - (equity - initial)",
            rec["total_pnl"] - (rec["equity"] - initial),
        ),
    )
    out = []
    for etype, failed, field_name, value in checks:
        if failed:
            out.append(
                make_alert(
                    etype,
                    key_parts=(common["account_id"], rec["snapshot_id"]),
                    occurred_at=rec["valued_at"],
                    session=sess,
                    **common,
                    title=f"{PAPER_TAG}: safety invariant violated ({field_name})",
                    message=f"{PAPER_TAG}. Invariant failed on snapshot {rec['snapshot_id']}: "
                    f"{field_name} = {value}. No automatic action is taken.",
                    payload={
                        "snapshot_id": rec["snapshot_id"],
                        "field": field_name,
                        "value": value,
                    },
                )
            )
    return out


# ── market data ──


def missing_session_alerts(
    bar_index: pd.DatetimeIndex,
    expected_opens: pd.DatetimeIndex,
    *,
    instrument: str,
    since: pd.Timestamp | None = None,
    app_version: str = "",
) -> list[AlertEvent]:
    """Calendar sessions inside [first bar, last bar] with no stored bar."""
    inside = expected_opens[(expected_opens >= bar_index[0]) & (expected_opens <= bar_index[-1])]
    out = []
    for ts in inside.difference(bar_index):
        if not _from(ts, since):
            continue
        out.append(
            make_alert(
                "DATA_MISSING_SESSION",
                key_parts=(instrument, session_of(ts)),
                occurred_at=iso(ts),
                session=session_of(ts),
                instrument=instrument,
                app_version=app_version,
                title=f"{instrument}: expected session {session_of(ts)} has no stored bar",
                message=f"The XNYS calendar has a regular session on {session_of(ts)} inside the "
                "stored history, but no closed bar is stored for it.",
                payload={"session_open": iso(ts)},
            )
        )
    return out


def stale_data_alert(
    *,
    instrument: str,
    last_bar_session: date,
    expected_session: date | None,
    missing_sessions: int,
    detected_at: str,
    app_version: str = "",
) -> AlertEvent | None:
    """One alert per staleness episode (keyed by the last stored session), not per refresh."""
    if missing_sessions <= 0:
        return None
    return make_alert(
        "DATA_STALE",
        key_parts=(instrument, last_bar_session.isoformat()),
        occurred_at=detected_at,
        session=None,
        instrument=instrument,
        app_version=app_version,
        title=f"{instrument}: stored data is stale",
        message=f"Last stored session {last_bar_session}; latest completed XNYS session "
        f"{expected_session}; {missing_sessions} completed session(s) not ingested.",
        payload={
            "last_bar_session": last_bar_session.isoformat(),
            "expected_session": None if expected_session is None else expected_session.isoformat(),
            "missing_sessions": missing_sessions,
        },
    )


def ingestion_alerts(
    runs: Iterable[dict[str, Any]], *, instrument: str, app_version: str = ""
) -> list[AlertEvent]:
    out = []
    for r in runs:
        if r["status"] == "succeeded":
            continue
        out.append(
            make_alert(
                "DATA_INGESTION_FAILED",
                key_parts=(instrument, str(r["run_id"])),
                occurred_at=iso(r["finished_at"] or r["started_at"]),
                instrument=instrument,
                app_version=app_version,
                title=f"{instrument}: ingestion run {r['run_id']} {r['status']}",
                message=f"Ingestion run {r['run_id']} ({r['provider']}) ended with status "
                f"{r['status']}: {r['error'] or 'no error message'}.",
                payload={k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in r.items()},
            )
        )
    return out


def quality_event_alerts(
    events: Iterable[dict[str, Any]], *, instrument: str, app_version: str = ""
) -> list[AlertEvent]:
    out = []
    for e in events:
        provider_failure = e["check_name"] == "provider_failure"
        out.append(
            make_alert(
                "DATA_PROVIDER_FAILURE" if provider_failure else "DATA_QUALITY_EVENT",
                key_parts=(instrument, str(e["id"])),
                occurred_at=iso(e["detected_at"]),
                instrument=instrument,
                app_version=app_version,
                session=None if e["bar_ts"] is None else session_of(e["bar_ts"]),
                severity=None
                if provider_failure
                else QUALITY_SEVERITY.get(str(e["severity"]), INFO),
                title=f"{instrument}: data-quality event {e['check_name']} ({e['severity']})",
                message=f"{e['description']} Action taken: {e['action_taken']}.",
                payload={
                    "event_id": e["id"],
                    "check_name": e["check_name"],
                    "severity": e["severity"],
                    "action_taken": e["action_taken"],
                    "bar_ts": None if e["bar_ts"] is None else iso(e["bar_ts"]),
                    "ingestion_run_id": e["ingestion_run_id"],
                    "details": e["details"],
                },
            )
        )
    return out
