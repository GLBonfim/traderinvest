"""Lightweight local scheduler (ADR-0024): a single long-running process, started by the user.

Every `scheduler_poll_seconds` it wakes, records a heartbeat and decides whether a pipeline run is
due — a new XNYS session became eligible (official close + grace), or a late-data retry interval
elapsed. When due it runs the pipeline in `scheduled` mode; the pipeline's lock, checkpoint and
the subsystems' idempotency make extra wake-ups harmless (no-op), so the polling frequency never
changes any financial behaviour. Time and sleep are injected, so tests never wait.

No OS scheduler, service, cron, APScheduler, Celery or Redis is installed or configured.
"""

import os
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.data.calendar import TradingCalendar
from app.operations import models as m
from app.operations import sessions as cal_
from app.operations.config import OperationsConfig
from app.operations.lock import LockReleaseError
from app.operations.pipeline import Pipeline
from app.operations.store import OpsStore, OpsStoreError

log = get_logger("operations.scheduler")

# Failure policy (ADR-0033). The pipeline already turns stage failures into results; an
# exception reaching the scheduler means something outside the stages went wrong.
RUN_ERROR = "RUN_ERROR"  # tick/heartbeat status of a scheduled run that raised
MAX_CONSECUTIVE_FAILURES = 3  # halt after this many failed runs in a row
FAILURE_BACKOFF_CAP_S = 3600  # retry delay: poll * 2**(n-1) seconds, capped
# Errors after which continuing is not safe: lock state or operations store uncertain.
FATAL_ERRORS: tuple[type[BaseException], ...] = (LockReleaseError, OpsStoreError)


def safe_error(exc: BaseException) -> str:
    """Single line, credentials in URLs masked, bounded length."""
    msg = re.sub(r"://[^/@\s]*@", "://***@", " ".join(str(exc).split()))
    return f"{type(exc).__name__}: {msg}"[:300]


@dataclass
class TickResult:
    at: str
    ran: bool
    reason: str
    run_status: str | None = None
    next_check: str = ""
    error: str | None = None


class Scheduler:
    def __init__(
        self,
        config: OperationsConfig,
        pipeline_factory: Callable[[], Pipeline],
        store: OpsStore,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        import time

        self.cfg = config
        self.make_pipeline = pipeline_factory
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or time.sleep
        self.cal = calendar or TradingCalendar(config.exchange)
        self.started_at = self.clock().isoformat()
        self.consecutive_failures = 0
        self.retry_not_before: datetime | None = None
        self.halted: str | None = None
        self.last_error: dict[str, Any] | None = None

    def due(self, now: datetime) -> tuple[bool, str]:
        """Cheap decision from the calendar and the checkpoint (no database access)."""
        eligible = cal_.latest_eligible_session(self.cal, now, self.cfg.grace_minutes)
        checkpoint = self.store.last_completed_session
        if eligible is None:
            return False, "no eligible session"
        if checkpoint is None:
            return True, "no checkpoint yet"
        if eligible <= checkpoint:
            return False, f"latest eligible session {eligible} already completed"
        first = cal_.sessions_after(self.cal, checkpoint, eligible)[0]
        a = self.store.data_attempts(first)
        if a.get("exhausted_at_eligible") == eligible.isoformat():
            return (
                False,
                f"data for {first} not delivered after {a['attempts']} attempts; "
                "waiting for the next session",
            )
        if a.get("attempts"):
            nxt = datetime.fromisoformat(a["last_attempt_at"]) + timedelta(
                minutes=self.cfg.data_retry_interval_minutes
            )
            if now < nxt:
                return False, f"late-data retry for {first} due at {nxt.isoformat()}"
        return True, f"session {first} eligible"

    def tick(self) -> TickResult:
        self.store.reload()  # pipeline runs write through their own store object
        now = self.clock()
        due, reason = self.due(now)
        status = None
        error = None
        if due and self.retry_not_before is not None and now < self.retry_not_before:
            due = False
            reason = (
                f"backing off after {self.consecutive_failures} failed run(s); "
                f"next attempt not before {self.retry_not_before.isoformat()}"
            )
        if due:
            # Only ordinary exceptions are handled; KeyboardInterrupt/SystemExit propagate.
            try:
                result = self.make_pipeline().run(mode="scheduled", now=now)
            except Exception as exc:
                status, error = RUN_ERROR, safe_error(exc)
                self._record_failure(now, exc, error)
            else:
                status = result.status
                self.consecutive_failures = 0
                self.retry_not_before = None
                log.info(
                    "operations.scheduled_run",
                    run_id=result.run_id,
                    status=status,
                    targets=result.target_sessions,
                )
        nxt = now + timedelta(seconds=self.cfg.scheduler_poll_seconds)
        self.store.heartbeat(self._heartbeat(now, nxt, reason, status))
        return TickResult(now.isoformat(), due, reason, status, nxt.isoformat(), error)

    def _record_failure(self, now: datetime, exc: Exception, error: str) -> None:
        """One log line per failed run; then either a bounded back-off or a halt."""
        self.consecutive_failures += 1
        self.last_error = {"at": now.isoformat(), "error": error}
        if isinstance(exc, FATAL_ERRORS):
            self.halted = f"fatal error ({type(exc).__name__}); operator action required"
        elif self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.halted = f"{self.consecutive_failures} consecutive failed runs"
        else:
            delay = min(
                self.cfg.scheduler_poll_seconds * 2 ** (self.consecutive_failures - 1),
                FAILURE_BACKOFF_CAP_S,
            )
            self.retry_not_before = now + timedelta(seconds=delay)
        log.error(
            "operations.scheduled_run_failed",
            error=error,
            consecutive_failures=self.consecutive_failures,
            retry_not_before=self.retry_not_before.isoformat() if self.retry_not_before else None,
            halted=self.halted,
        )

    def _heartbeat(
        self, now: datetime, nxt: datetime, reason: str, status: str | None
    ) -> dict[str, Any]:
        last = self.store.state.get("scheduler", {})
        return {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": self.started_at,
            "last_check": now.isoformat(),
            "next_check": nxt.isoformat(),
            "last_reason": reason,
            "last_run_status": status or last.get("last_run_status"),
            "next_eligible_at": cal_.next_eligibility(
                self.cal, now, self.cfg.grace_minutes
            ).isoformat(),
            "poll_seconds": self.cfg.scheduler_poll_seconds,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "retry_not_before": self.retry_not_before.isoformat()
            if self.retry_not_before
            else None,
            "halted": self.halted,
        }

    def run_forever(self, max_ticks: int | None = None) -> list[TickResult]:
        results: list[TickResult] = []
        log.info("operations.scheduler_started", poll_seconds=self.cfg.scheduler_poll_seconds)
        try:
            while max_ticks is None or len(results) < max_ticks:
                results.append(self.tick())
                if self.halted:
                    log.critical("operations.scheduler_halted", reason=self.halted)
                    break
                if max_ticks is None or len(results) < max_ticks:
                    self.sleep(self.cfg.scheduler_poll_seconds)
        except KeyboardInterrupt:  # operator stop (Ctrl+C): end the loop, never continue
            log.info("operations.scheduler_stopped", reason="KeyboardInterrupt")
        return results


def scheduler_status(store: OpsStore | None, now: datetime, poll_seconds: int) -> dict[str, Any]:
    """Alive if the heartbeat is recent (<= 2 poll intervals). No process management."""
    hb = (store.state.get("scheduler") or {}) if store else {}
    if not hb:
        return {"status": "not running (no heartbeat)", **hb}
    if hb.get("halted"):
        return {"status": f"halted: {hb['halted']}", **hb}
    age = (now - datetime.fromisoformat(hb["last_check"])).total_seconds()
    alive = age <= 2 * poll_seconds + 60
    return {
        "status": "running" if alive else "stopped (stale heartbeat)",
        "heartbeat_age_seconds": round(age),
        **hb,
    }


__all__ = ["Scheduler", "TickResult", "m", "scheduler_status"]
