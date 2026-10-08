"""Deterministic fake `Services` for pipeline tests: durable effects are recorded so duplicates
are detectable; failures and crashes can be injected at any stage."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.alerts.models import AlertEvent

UTC_ = UTC


class Crash(BaseException):
    """Not an `Exception`: escapes the pipeline's stage error handling like a real crash."""


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


@dataclass
class FakeServices:
    stored: set[date] = field(default_factory=set)
    published: set[date] = field(default_factory=set)  # what the "provider" can deliver
    invalid: set[date] = field(default_factory=set)
    accounts: dict[str, set[date]] = field(
        default_factory=lambda: {"acct_a": set(), "acct_b": set()}
    )
    failing_accounts: set[str] = field(default_factory=set)
    fail: dict[str, Exception] = field(default_factory=dict)  # stage -> error (Exception)
    crash: dict[tuple[str, date], int] = field(default_factory=dict)  # (stage, session) -> times
    precheck_ok: bool = True
    calls: list[tuple[str, Any]] = field(default_factory=list)
    effects: list[tuple[str, str, date]] = field(default_factory=list)  # durable side effects
    ops_alerts: list[AlertEvent] = field(default_factory=list)
    alert_sessions: list[date] = field(default_factory=list)

    def _maybe(self, stage: str, s: date | None) -> None:
        if s is not None and self.crash.get((stage, s), 0) > 0:
            self.crash[(stage, s)] -= 1
            raise Crash(f"crash in {stage} {s}")
        if stage in self.fail:
            raise self.fail[stage]

    def precheck(self) -> dict[str, dict[str, Any]]:
        self.calls.append(("precheck", None))
        return {
            "database": {"ok": self.precheck_ok, "critical": True, "detail": "x"},
            "paper_accounts": {"ok": True, "critical": False, "detail": "ok"},
        }

    def latest_stored_session(self) -> date | None:
        return max(self.stored) if self.stored else None

    def bar_exists(self, s: date) -> bool:
        return s in self.stored

    def ingest(self, s: date) -> dict[str, Any]:
        self.calls.append(("ingest", s))
        self._maybe("INGEST", s)
        inserted = 0
        if s in self.published and s not in self.stored:
            self.stored.add(s)
            self.effects.append(("bar", "SPY", s))
            inserted = 1
        return {"bars_inserted": inserted, "status": "succeeded"}

    def validate(self, s: date) -> dict[str, Any]:
        self.calls.append(("validate", s))
        self._maybe("VALIDATE", s)
        if s not in self.stored:
            return {"ok": False, "missing": True, "detail": f"no closed bar stored for {s}"}
        if s in self.invalid:
            return {"ok": False, "missing": False, "detail": "inconsistent OHLC"}
        return {"ok": True, "missing": False, "detail": "ok"}

    def paper(self, s: date) -> dict[str, Any]:
        self.calls.append(("paper", s))
        self._maybe("PAPER", s)
        out: dict[str, Any] = {"accounts": {}, "failed": []}
        for acct, done in self.accounts.items():
            if acct in self.failing_accounts:
                out["accounts"][acct] = {"status": "failed", "error": "LedgerError: broken"}
                out["failed"].append(acct)
                continue
            # an account processes, in order, every stored session <= s it has not processed
            todo = sorted(d for d in self.stored if d <= s and d not in done)
            for d in todo:
                done.add(d)
                self.effects.append(("paper", acct, d))
            out["accounts"][acct] = {"status": "ok", "processed": len(todo)}
        self._maybe("PAPER_AFTER_COMMIT", s)
        return out

    def alerts(self, s: date, now: datetime) -> dict[str, Any]:
        self.calls.append(("alerts", s))
        self._maybe("ALERTS", s)
        if s not in self.alert_sessions:
            self.alert_sessions.append(s)
        return {"new_alerts": 1}

    def health(self, now: datetime) -> dict[str, Any]:
        self.calls.append(("health", None))
        if "HEALTH" in self.fail:
            raise self.fail["HEALTH"]
        return {"overall": "ok", "components": {}}

    def paper_accounts(self) -> list[dict[str, Any]]:
        return [
            {"account_id": a, "last_session": max(d).isoformat() if d else None}
            for a, d in self.accounts.items()
        ]

    def emit_ops_alerts(self, alerts: list[AlertEvent], now: datetime) -> None:
        self.ops_alerts.extend(alerts)

    def git_commit(self) -> str:
        return "test"

    def mutating_calls(self) -> list[tuple[str, Any]]:
        return [c for c in self.calls if c[0] in ("ingest", "paper", "alerts")]
