"""Versioned, validated, deterministic serialisation of `PaperState` (restart safety).

Floats are written with Python's shortest round-trip repr, so a save/load cycle restores every
value bit-exactly; NaN is written as null (strict JSON: `allow_nan=False`). Timestamps are ISO
8601 UTC. No wall-clock time is stored here, so the same history always produces the same
bytes. Any missing/unknown field, wrong schema version, identity or configuration mismatch, or
invalid value raises `StateError` — state is never silently repaired.
"""

import json
import math
from typing import Any

import pandas as pd

from app.paper.account import Account
from app.paper.config import PAPER_VERSION, PaperConfig
from app.paper.models import Instruction
from app.paper.trader import PaperState
from app.risk.manager import RiskManager

STATE_SCHEMA_VERSION = 1
_OPEN_TRADE_FLOATS = (
    "entry_equity",
    "entry_price",
    "buy_notional",
    "buy_costs",
    "sell_notional",
    "sell_costs",
)


class StateError(RuntimeError):
    """Paper state is missing, corrupt or incompatible; nothing was modified."""


def _f(x: float) -> float | None:
    x = float(x)
    if math.isinf(x):
        raise StateError("infinite value in paper state")
    return None if math.isnan(x) else x


def _uf(x: Any) -> float:
    if x is None:
        return math.nan
    if not isinstance(x, int | float) or isinstance(x, bool):
        raise StateError(f"expected a number, got {type(x).__name__}")
    return float(x)


def _ts(t: pd.Timestamp | None) -> str | None:
    return None if t is None else pd.Timestamp(t).tz_convert("UTC").isoformat()


def _uts(s: Any) -> pd.Timestamp | None:
    if s is None:
        return None
    if not isinstance(s, str):
        raise StateError("timestamp must be an ISO string")
    t = pd.Timestamp(s)
    if t.tzinfo is None:
        raise StateError("timestamp must be timezone-aware")
    return t.tz_convert("UTC")


def _req_ts(s: Any) -> pd.Timestamp:
    t = _uts(s)
    if t is None:
        raise StateError("required timestamp is missing")
    return t


def to_dict(state: PaperState) -> dict[str, Any]:
    a, rm = state.account, state.risk
    p = state.pending
    ot = state.open_trade
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "paper_version": PAPER_VERSION,
        "identity": {
            "account_id": state.account_id,
            "strategy_id": state.strategy_id,
            "strategy_version": state.strategy_version,
            "instrument": state.instrument,
            "timeframe": state.timeframe,
            "config_fingerprint": state.config_fingerprint,
        },
        "account": {
            "initial_capital": _f(a.initial_capital),
            "cash": _f(a.cash),
            "shares": _f(a.shares),
            "basis_notional": _f(a.basis_notional),
            "basis_costs": _f(a.basis_costs),
            "realized_pnl": _f(a.realized_pnl),
            "cumulative_costs": _f(a.cumulative_costs),
        },
        "risk": {
            "drawdown": {
                "hwm": _f(rm.dd.hwm),
                "max_drawdown": _f(rm.dd.max_drawdown),
                "duration": rm.dd.duration,
                "lock_remaining": rm.dd.lock_remaining,
                "state": rm.dd.state,
            },
            "session": {
                "last_equity": _f(rm.sess.last_equity),
                "lock_remaining": rm.sess.lock_remaining,
                "consecutive_losses": rm.sess.consecutive_losses,
            },
            "stop": _f(rm.stop),
            "stop_lockout": rm.stop_lockout,
            "current_target": _f(rm.current_target),
        },
        "pending": None
        if p is None
        else {
            "decision_id": p.decision_id,
            "observed_at": _ts(p.observed_at),
            "scheduled_for": _ts(p.scheduled_for),
            "strategy_requested_target": _f(p.strategy_requested_target),
            "risk_approved_target": _f(p.risk_approved_target),
            "pending_kind": p.pending_kind,
            "atr_at_decision": _f(p.atr_at_decision),
        },
        "progress": {
            "broker_last_session": _ts(state.broker_last_session),
            "last_session": _ts(state.last_session),
            "session_seq": state.session_seq,
            "last_requested": _f(state.last_requested),
            "last_decision_id": state.last_decision_id,
            "last_input_fingerprint": state.last_input_fingerprint,
            "trade_count": state.trade_count,
        },
        "open_trade": None
        if ot is None
        else {
            **{k: _f(ot[k]) for k in _OPEN_TRADE_FLOATS},
            "entry_time": _ts(ot["entry_time"]),
            "entry_seq": ot["entry_seq"],
            "fills": ot["fills"],
        },
    }


def dumps(state: PaperState, ledger: dict[str, Any]) -> str:
    return json.dumps(
        {**to_dict(state), "ledger": ledger}, sort_keys=True, indent=2, allow_nan=False
    )


def _keys(d: Any, expected: set[str], where: str) -> dict[str, Any]:
    if not isinstance(d, dict) or set(d) != expected:
        raise StateError(f"{where}: unexpected or missing fields")
    return d


def _int(x: Any, where: str, minimum: int = 0) -> int:
    if not isinstance(x, int) or isinstance(x, bool) or x < minimum:
        raise StateError(f"{where}: expected an integer >= {minimum}")
    return x


def from_dict(d: dict[str, Any], config: PaperConfig) -> tuple[PaperState, dict[str, Any]]:
    """Validates and restores (state, ledger pointer)."""
    top = {
        "schema_version",
        "paper_version",
        "identity",
        "account",
        "risk",
        "pending",
        "progress",
        "open_trade",
        "ledger",
    }
    _keys(d, top, "state")
    if d["schema_version"] != STATE_SCHEMA_VERSION:
        raise StateError(f"unsupported state schema version {d['schema_version']!r}")
    ident = _keys(
        d["identity"],
        {
            "account_id",
            "strategy_id",
            "strategy_version",
            "instrument",
            "timeframe",
            "config_fingerprint",
        },
        "identity",
    )
    if ident["config_fingerprint"] != config.fingerprint():
        raise StateError("paper state was created with a different configuration")

    acc = _keys(
        d["account"],
        {
            "initial_capital",
            "cash",
            "shares",
            "basis_notional",
            "basis_costs",
            "realized_pnl",
            "cumulative_costs",
        },
        "account",
    )
    account = Account(**{k: _uf(v) for k, v in acc.items()})
    for k, v in vars(account).items():
        if not math.isfinite(v):
            raise StateError(f"account.{k} must be finite")
    if account.initial_capital != float(config.backtest.initial_capital):
        raise StateError("initial capital differs from the configuration")
    if account.shares < 0 or account.cash < 0:
        raise StateError("negative position or cash in stored state")

    r = _keys(d["risk"], {"drawdown", "session", "stop", "stop_lockout", "current_target"}, "risk")
    rm = RiskManager(config.risk, account.initial_capital)
    dd = _keys(
        r["drawdown"],
        {"hwm", "max_drawdown", "duration", "lock_remaining", "state"},
        "risk.drawdown",
    )
    rm.dd.hwm, rm.dd.max_drawdown = _uf(dd["hwm"]), _uf(dd["max_drawdown"])
    rm.dd.duration = _int(dd["duration"], "risk.drawdown.duration")
    rm.dd.lock_remaining = _int(dd["lock_remaining"], "risk.drawdown.lock_remaining")
    if dd["state"] not in ("NORMAL", "WARNING", "LOCKED"):
        raise StateError("risk.drawdown.state invalid")
    rm.dd.state = dd["state"]
    ss = _keys(
        r["session"], {"last_equity", "lock_remaining", "consecutive_losses"}, "risk.session"
    )
    rm.sess.last_equity = _uf(ss["last_equity"])
    rm.sess.lock_remaining = _int(ss["lock_remaining"], "risk.session.lock_remaining")
    rm.sess.consecutive_losses = _int(ss["consecutive_losses"], "risk.session.consecutive")
    rm.stop = _uf(r["stop"])
    if not isinstance(r["stop_lockout"], bool):
        raise StateError("risk.stop_lockout must be boolean")
    rm.stop_lockout = r["stop_lockout"]
    rm.current_target = _uf(r["current_target"])
    if not 0 <= rm.current_target <= config.max_gross_exposure + 1e-12:
        raise StateError("risk.current_target out of [0, max exposure]")

    pending = None
    if d["pending"] is not None:
        p = _keys(
            d["pending"],
            {
                "decision_id",
                "observed_at",
                "scheduled_for",
                "strategy_requested_target",
                "risk_approved_target",
                "pending_kind",
                "atr_at_decision",
            },
            "pending",
        )
        pending = Instruction(
            str(p["decision_id"]),
            _req_ts(p["observed_at"]),
            _req_ts(p["scheduled_for"]),
            _uf(p["strategy_requested_target"]),
            _uf(p["risk_approved_target"]),
            str(p["pending_kind"]),
            _uf(p["atr_at_decision"]),
        )
        if pending.pending_kind not in ("none", "entry", "exit", "rebalance"):
            raise StateError("pending.pending_kind invalid")

    pr = _keys(
        d["progress"],
        {
            "broker_last_session",
            "last_session",
            "session_seq",
            "last_requested",
            "last_decision_id",
            "last_input_fingerprint",
            "trade_count",
        },
        "progress",
    )
    ot: dict[str, Any] | None = None
    if d["open_trade"] is not None:
        o = _keys(
            d["open_trade"],
            {
                "entry_equity",
                "entry_price",
                "buy_notional",
                "buy_costs",
                "sell_notional",
                "sell_costs",
                "entry_time",
                "entry_seq",
                "fills",
            },
            "open_trade",
        )
        ot = {k: _uf(o[k]) for k in _OPEN_TRADE_FLOATS}
        ot.update(
            entry_time=_req_ts(o["entry_time"]),
            entry_seq=_int(o["entry_seq"], "open_trade.entry_seq"),
            fills=_int(o["fills"], "open_trade.fills"),
        )
    if (ot is not None) != account.holding:
        raise StateError("open round trip and position disagree")

    state = PaperState(
        account_id=str(ident["account_id"]),
        strategy_id=str(ident["strategy_id"]),
        strategy_version=str(ident["strategy_version"]),
        instrument=str(ident["instrument"]),
        timeframe=str(ident["timeframe"]),
        config_fingerprint=str(ident["config_fingerprint"]),
        account=account,
        risk=rm,
        broker_last_session=_uts(pr["broker_last_session"]),
        pending=pending,
        last_requested=_uf(pr["last_requested"]),
        session_seq=_int(pr["session_seq"], "progress.session_seq"),
        last_session=_uts(pr["last_session"]),
        last_decision_id=str(pr["last_decision_id"]),
        last_input_fingerprint=str(pr["last_input_fingerprint"]),
        open_trade=ot,
        trade_count=_int(pr["trade_count"], "progress.trade_count"),
    )
    ledger = _keys(d["ledger"], {"seq", "last_hash"}, "ledger")
    _int(ledger["seq"], "ledger.seq")
    if not isinstance(ledger["last_hash"], str):
        raise StateError("ledger.last_hash must be a string")
    return state, ledger


def loads(text: str, config: PaperConfig) -> tuple[PaperState, dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(f"paper state is not valid JSON: {exc}") from exc
    return from_dict(data, config)
