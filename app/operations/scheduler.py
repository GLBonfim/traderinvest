"""Lightweight local scheduler (ADR-0024): a single long-running process, started by the user.

Every `scheduler_poll_seconds` it wakes, records a heartbeat and decides whether a pipeline run is
due — a new XNYS session became eligible (official close + grace), or a late-data retry interval
elapsed. When due it runs the pipeline in `scheduled` mode; the pipeline's lock, checkpoint and
the subsystems' idempotency make extra wake-ups harmless (no-op), so the polling frequency never
changes any financial behaviour. Time and sleep are injected, so tests never wait.

No OS scheduler, service, cron, APScheduler, Celery or Redis is installed or configured.
"""

import os
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
from app.operations.pipeline import Pipeline
from app.operations.store import OpsStore

log = get_logger("operations.scheduler")


@dataclass
class TickResult:
    at: str
    ran: bool
    reason: str
    run_status: str | None = None
    next_check: str = ""


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
        if due:
            result = self.make_pipeline().run(mode="scheduled", now=now)
            status = result.status
            log.info(
                "operations.scheduled_run",
                run_id=result.run_id,
                status=status,
                targets=result.target_sessions,
            )
        nxt = now + timedelta(seconds=self.cfg.scheduler_poll_seconds)
        self.store.heartbeat(self._heartbeat(now, nxt, reason, status))
        return TickResult(now.isoformat(), due, reason, status, nxt.isoformat())

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
        }

    def run_forever(self, max_ticks: int | None = None) -> list[TickResult]:
        results: list[TickResult] = []
        log.info("operations.scheduler_started", poll_seconds=self.cfg.scheduler_poll_seconds)
        try:
            while max_ticks is None or len(results) < max_ticks:
                results.append(self.tick())
                if max_ticks is None or len(results) < max_ticks:
                    self.sleep(self.cfg.scheduler_poll_seconds)
        except KeyboardInterrupt:
            log.info("operations.scheduler_stopped")
        return results


def scheduler_status(store: OpsStore | None, now: datetime, poll_seconds: int) -> dict[str, Any]:
    """Alive if the heartbeat is recent (<= 2 poll intervals). No process management."""
    hb = (store.state.get("scheduler") or {}) if store else {}
    if not hb:
        return {"status": "not running (no heartbeat)", **hb}
    age = (now - datetime.fromisoformat(hb["last_check"])).total_seconds()
    alive = age <= 2 * poll_seconds + 60
    return {
        "status": "running" if alive else "stopped (stale heartbeat)",
        "heartbeat_age_seconds": round(age),
        **hb,
    }


__all__ = ["Scheduler", "TickResult", "m", "scheduler_status"]
