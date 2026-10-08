"""Daily operations pipeline: pure orchestration of existing subsystems.

    PRECHECK -> for each target session (oldest first): INGEST -> VALIDATE -> PAPER -> ALERTS
             -> HEALTH -> COMPLETE

This module decides WHAT runs and in WHICH order; every action is delegated to `Services`
(the real adapter in `services.py` calls the Phase 2 ingestion, the stored-bar checks, the
Phase 12 paper store, the Phase 14 alert run and the health checks). It contains no indicator,
strategy, risk, accounting or alert-rule logic.

Target sessions: XNYS sessions in (checkpoint, latest eligible], where a session is eligible
once its official close + grace has passed and the checkpoint is the last fully completed
session (initially: the session before the latest stored bar, so the latest stored session gets
a verification pass). At most `max_catch_up_sessions` per run, oldest first — never skipped.

Failure policy (per session):
    INGEST fails            -> session FAILED; later sessions not attempted; paper untouched
    VALIDATE: bar missing   -> WAITING_FOR_DATA (bounded attempts) -> DATA_MISSING when exhausted
    VALIDATE: bar invalid   -> session FAILED
    PAPER: stage error      -> session FAILED (checkpoint not advanced)
    PAPER: account error    -> isolated: other accounts processed; session completes with warnings
                               (the failing account catches up later through its own store)
    ALERTS fails            -> warning only; nothing is rolled back
    HEALTH fails            -> warning only
"""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from app import __version__
from app.alerts.models import AlertEvent, make_alert
from app.data.calendar import TradingCalendar
from app.operations import models as m
from app.operations import sessions as cal_
from app.operations.config import OperationsConfig
from app.operations.lock import LockHeldError, PipelineLock
from app.operations.store import OpsStore


class Services(Protocol):
    def precheck(self) -> dict[str, dict[str, Any]]: ...  # name -> {ok, critical, detail}
    def latest_stored_session(self) -> date | None: ...
    def bar_exists(self, session: date) -> bool: ...
    def ingest(self, session: date) -> dict[str, Any]: ...
    def validate(self, session: date) -> dict[str, Any]: ...  # {ok, missing, detail}
    def paper(self, session: date) -> dict[str, Any]: ...  # {accounts: {...}, failed: [...]}
    def alerts(self, session: date, now: datetime) -> dict[str, Any]: ...
    def health(self, now: datetime) -> dict[str, Any]: ...
    def paper_accounts(self) -> list[dict[str, Any]]: ...
    def emit_ops_alerts(self, alerts: list[AlertEvent], now: datetime) -> None: ...
    def git_commit(self) -> str: ...


@dataclass(frozen=True)
class Plan:
    now: str
    latest_eligible: date | None
    latest_stored: date | None
    checkpoint: date | None
    pending: list[date]
    targets: list[date]
    backlog: int


class Pipeline:
    def __init__(
        self,
        config: OperationsConfig,
        services: Services,
        store: OpsStore,
        *,
        clock: Callable[[], datetime] | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self.cfg = config
        self.svc = services
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.cal = calendar or TradingCalendar(config.exchange)

    # ── planning (read-only) ──

    def plan(self, now: datetime) -> Plan:
        eligible = cal_.latest_eligible_session(self.cal, now, self.cfg.grace_minutes)
        stored = self.svc.latest_stored_session()
        checkpoint = self.store.last_completed_session
        if checkpoint is None and stored is not None:
            checkpoint = cal_.previous_session(self.cal, min(stored, eligible or stored))
        pending = [] if eligible is None else cal_.sessions_after(self.cal, checkpoint, eligible)
        targets = pending[: self.cfg.max_catch_up_sessions]
        return Plan(
            now.isoformat(),
            eligible,
            stored,
            checkpoint,
            pending,
            targets,
            len(pending) - len(targets),
        )

    def dry_run(self, now: datetime | None = None) -> m.RunResult:
        """Shows what a run would do. Writes nothing (no lock, no history, no state)."""
        now = now or self.clock()
        t0 = self.clock().isoformat()
        checks = self.svc.precheck()
        pre = m.StageResult(
            m.PRECHECK,
            m.OK if _precheck_ok(checks) else m.FAILED,
            t0,
            self.clock().isoformat(),
            result=checks,
        )
        plan = self.plan(now)
        lock = PipelineLock(self.store.root).owner()
        per_session = [
            {
                "session": s.isoformat(),
                "ingest": "skip (bar stored)" if self.svc.bar_exists(s) else "ingest from provider",
                "stages": [m.INGEST, m.VALIDATE, m.PAPER, m.ALERTS],
            }
            for s in plan.targets
        ]
        return m.RunResult(
            run_id="dry-run",
            status=m.DRY_RUN,
            started_at=t0,
            completed_at=self.clock().isoformat(),
            mode="dry_run",
            target_sessions=[s.isoformat() for s in plan.targets],
            precheck=pre,
            plan={
                **_plan_dict(plan),
                "sessions": per_session,
                "paper_accounts": self.svc.paper_accounts(),
                "alerts_would_run": True,
                "lock": lock,
                "stages_after_sessions": [m.HEALTH, m.COMPLETE],
            },
            app_version=__version__,
            config_fingerprint=self.cfg.fingerprint(),
        )

    # ── execution ──

    def run(self, *, mode: str = "manual", now: datetime | None = None) -> m.RunResult:
        if mode == "dry_run":
            return self.dry_run(now)
        now = now or self.clock()
        started = self.clock().isoformat()
        lock = PipelineLock(self.store.root)
        try:
            lock.acquire()
        except LockHeldError as exc:
            self.store.bump("lock_conflicts")
            self._alert(
                "OPS_LOCK_CONFLICT",
                ("lock", str(exc.owner.get("lock_id"))),
                now,
                "Operations pipeline lock conflict",
                "Another pipeline instance holds the lock; this run did nothing.",
                {"owner": {k: exc.owner.get(k) for k in ("host", "pid", "acquired_at", "purpose")}},
            )
            return self._result(m.LOCKED, started, mode, [], warnings=[str(exc)])
        try:
            return self._run_locked(mode, now, started, lock)
        finally:
            lock.release()

    def _run_locked(
        self, mode: str, now: datetime, started: str, lock: PipelineLock
    ) -> m.RunResult:
        warnings: list[str] = []
        if lock.broken_stale:
            warnings.append(f"stale lock of dead process {lock.broken_stale.get('pid')} broken")
        pre = self._stage(m.PRECHECK, None, self.svc.precheck)
        if pre.status == m.OK and not _precheck_ok(pre.result):
            pre.status = m.FAILED
            pre.error = "critical precheck failed: " + ", ".join(
                k for k, v in pre.result.items() if v.get("critical") and not v.get("ok")
            )
        if pre.status == m.FAILED:
            run_id = self._run_id([], now)
            self.store.append(
                "run_started", run_id, started_at=started, mode=mode, target_sessions=[]
            )
            self.store.record_stage(run_id, pre)
            r = self._result(
                m.PRECHECK_FAILED,
                started,
                mode,
                [],
                run_id=run_id,
                precheck=pre,
                errors=[pre.error],
            )
            self.store.finish_run(r, failed=True)
            self._alert(
                "OPS_STAGE_FAILED",
                (m.PRECHECK, now.date().isoformat(), pre.error),
                now,
                "Operations precheck failed",
                f"Pipeline stopped before any work: {pre.error}.",
                {"checks": pre.result},
            )
            self._repeated_failure(r, now)
            return r

        plan = self.plan(now)
        if not plan.targets:
            self.store.bump("noop_runs")
            return self._result(
                m.NO_NEW_COMPLETED_SESSION,
                started,
                mode,
                [],
                precheck=pre,
                plan=_plan_dict(plan),
                warnings=warnings,
            )
        if mode == "scheduled" and not self._data_retry_due(plan.targets[0], plan, now):
            self.store.bump("not_due_runs")
            return self._result(m.NOT_DUE, started, mode, [], precheck=pre, plan=_plan_dict(plan))

        run_id = self._run_id(plan.targets, now)
        targets = [s.isoformat() for s in plan.targets]
        self.store.append(
            "run_started", run_id, started_at=started, mode=mode, target_sessions=targets
        )
        self.store.record_stage(run_id, pre)
        if plan.backlog:
            warnings.append(f"catch-up: {plan.backlog} more session(s) left for later runs")
            self._alert(
                "OPS_CATCH_UP_REQUIRED",
                ("catch_up", plan.latest_eligible),
                now,
                f"Operations catch-up: {len(plan.pending)} sessions pending",
                f"{len(plan.pending)} completed sessions are pending; this run processes "
                f"the oldest {len(plan.targets)} in order, the rest follow.",
                {"pending": [s.isoformat() for s in plan.pending]},
            )

        results: list[m.SessionResult] = []
        for s in plan.targets:
            sr = self._session(run_id, s, plan, now)
            results.append(sr)
            self.store.record_session(run_id, sr)
            warnings += [
                f"{s}: {st.stage} {st.error}"
                for st in sr.stages
                if st.status == m.WARNING and st.error
            ]
            if sr.status in (m.SESSION_FAILED, m.SESSION_DATA_MISSING, m.SESSION_WAITING_FOR_DATA):
                break

        health = self._stage(m.HEALTH, None, lambda: self.svc.health(self.clock()))
        if health.status == m.FAILED:
            warnings.append(f"health: {health.error}")
        elif health.result.get("overall") not in (None, "ok"):
            health.status = m.WARNING
        self.store.record_stage(run_id, health)

        statuses = {r.status for r in results}
        errors = [
            f"{r.session}: {st.stage} {st.error}"
            for r in results
            for st in r.stages
            if st.status == m.FAILED
        ]
        if statuses & {m.SESSION_FAILED, m.SESSION_DATA_MISSING}:
            status = m.RUN_FAILED
        elif m.SESSION_WAITING_FOR_DATA in statuses:
            status = m.WAITING_FOR_DATA
        elif warnings or m.SESSION_COMPLETED_WITH_WARNINGS in statuses:
            status = m.COMPLETED_WITH_WARNINGS
        else:
            status = m.COMPLETED
        r = self._result(
            status,
            started,
            mode,
            targets,
            run_id=run_id,
            precheck=pre,
            sessions=results,
            health=health,
            warnings=warnings,
            errors=errors,
            plan=_plan_dict(plan),
        )
        complete = m.StageResult(
            m.COMPLETE,
            m.OK,
            self.clock().isoformat(),
            self.clock().isoformat(),
            result={"status": status},
        )
        self.store.record_stage(run_id, complete)
        self.store.finish_run(r, failed=status == m.RUN_FAILED)
        self._repeated_failure(r, now)
        return r

    def _session(self, run_id: str, s: date, plan: Plan, now: datetime) -> m.SessionResult:
        """One session; every stage is recorded once, with its final status."""
        sr = m.SessionResult(s.isoformat(), m.SESSION_COMPLETED)
        iso = s.isoformat()

        def done(st: m.StageResult) -> m.StageResult:
            sr.stages.append(st)
            self.store.record_stage(run_id, st)
            return st

        # INGEST (skipped when the bar is already durable: no duplicate network call or rows)
        if self.svc.bar_exists(s):
            t = self.clock().isoformat()
            done(m.StageResult(m.INGEST, m.SKIPPED, t, t, iso, {"reason": "bar already stored"}))
        else:
            ing = done(self._stage(m.INGEST, iso, lambda: self.svc.ingest(s)))
            if ing.status == m.FAILED:
                sr.status = m.SESSION_FAILED
                self._stage_alert(m.INGEST, iso, ing.error, now, {"ingestion": ing.result})
                return sr

        # VALIDATE
        val = self._stage(m.VALIDATE, iso, lambda: self.svc.validate(s))
        if val.status == m.OK and not val.result.get("ok"):
            val.error = str(val.result.get("detail", "validation failed"))
            if val.result.get("missing"):
                limit = self.cfg.data_retry_attempts
                exhausted = self.store.data_attempts(s)["attempts"] + 1 >= limit
                n = self.store.record_data_attempt(
                    s,
                    now.isoformat(),
                    plan.latest_eligible.isoformat()
                    if exhausted and plan.latest_eligible
                    else None,
                )
                if n < limit:
                    val.status, sr.status = m.WARNING, m.SESSION_WAITING_FOR_DATA
                    val.error += f" (attempt {n}/{limit}; retry later)"
                else:
                    val.status, sr.status = m.FAILED, m.SESSION_DATA_MISSING
                    val.error += f" (attempt {n}/{limit}; giving up for now)"
                    self._alert(
                        "OPS_SESSION_DATA_MISSING",
                        ("data_missing", iso),
                        now,
                        f"Expected session {iso} has no usable bar",
                        f"The provider has not delivered a completed bar for {iso} after "
                        f"{n} attempts. Paper trading does not process this session.",
                        {"session": iso, "attempts": n, "detail": val.result},
                    )
            else:
                val.status, sr.status = m.FAILED, m.SESSION_FAILED
                self._stage_alert(m.VALIDATE, iso, val.error, now, val.result)
            done(val)
            return sr
        done(val)
        if val.status == m.FAILED:
            sr.status = m.SESSION_FAILED
            self._stage_alert(m.VALIDATE, iso, val.error, now, {})
            return sr

        # PAPER (all financial state changes happen inside the Phase 12 store, via RiskManager)
        pap = self._stage(m.PAPER, iso, lambda: self.svc.paper(s))
        failed_accounts = pap.result.get("failed", []) if pap.status == m.OK else []
        if failed_accounts:
            pap.status, pap.error = m.WARNING, f"account(s) failed: {', '.join(failed_accounts)}"
        done(pap)
        if pap.status == m.FAILED:
            sr.status = m.SESSION_FAILED
            self._stage_alert(m.PAPER, iso, pap.error, now, {})
            return sr
        for acct in failed_accounts:
            self._stage_alert(
                m.PAPER,
                iso,
                pap.result["accounts"][acct].get("error", ""),
                now,
                {"account_id": acct},
                account=acct,
            )

        # ALERTS (observes the state produced above; a failure never rolls anything back)
        al = self._stage(m.ALERTS, iso, lambda: self.svc.alerts(s, now))
        if al.status == m.FAILED:
            al.status = m.WARNING
        done(al)

        self.store.mark_session_completed(s)
        if any(st.status == m.WARNING for st in sr.stages):
            sr.status = m.SESSION_COMPLETED_WITH_WARNINGS
        return sr

    # ── helpers ──

    def _data_retry_due(self, first: date, plan: Plan, now: datetime) -> bool:
        a = self.store.data_attempts(first)
        if not a.get("attempts"):
            return True
        if a.get("exhausted_at_eligible"):
            # attempts exhausted: retry automatically only once a newer session is eligible
            return bool(
                plan.latest_eligible
                and plan.latest_eligible.isoformat() != a["exhausted_at_eligible"]
            )
        last = datetime.fromisoformat(a["last_attempt_at"])
        return now - last >= timedelta(minutes=self.cfg.data_retry_interval_minutes)

    def _stage(
        self, stage: str, session: str | None, fn: Callable[[], dict[str, Any]]
    ) -> m.StageResult:
        t0 = self.clock().isoformat()
        try:
            res = fn()
            return m.StageResult(stage, m.OK, t0, self.clock().isoformat(), session, res)
        except Exception as exc:
            return m.StageResult(
                stage,
                m.FAILED,
                t0,
                self.clock().isoformat(),
                session,
                {},
                f"{type(exc).__name__}: {str(exc)[:300]}",
            )

    def _alert(
        self,
        etype: str,
        key: tuple[Any, ...],
        now: datetime,
        title: str,
        message: str,
        payload: dict[str, Any],
        account: str | None = None,
    ) -> None:
        alert = make_alert(
            etype,
            key_parts=tuple(None if k is None else str(k) for k in key),
            occurred_at=now.isoformat(),
            title=title,
            message=message,
            payload=payload,
            instrument=self.cfg.symbol,
            account_id=account,
            app_version=__version__,
        )
        with contextlib.suppress(Exception):  # alerting must never break operations
            self.svc.emit_ops_alerts([alert], now)

    def _stage_alert(
        self,
        stage: str,
        session: str,
        error: str,
        now: datetime,
        payload: dict[str, Any],
        account: str | None = None,
    ) -> None:
        self._alert(
            "OPS_STAGE_FAILED",
            (stage, session, account),
            now,
            f"Operations {stage} failed for session {session}"
            + (f" (account {account})" if account else ""),
            f"Stage {stage} failed for session {session}: {error}. Completed work is "
            "kept; the session is retried by a later run.",
            {"stage": stage, "session": session, "error": error, **payload},
            account=account,
        )

    def _repeated_failure(self, r: m.RunResult, now: datetime) -> None:
        n = int(self.store.state["consecutive_failures"])
        if n == self.cfg.repeated_failure_threshold:
            self._alert(
                "OPS_REPEATED_FAILURE",
                ("repeated", r.run_id),
                now,
                f"Operations pipeline failed {n} times in a row",
                f"The last {n} pipeline runs failed (latest {r.run_id}: {r.status}).",
                {"consecutive_failures": n, "last_run": r.run_id, "errors": r.errors},
            )

    def _run_id(self, targets: list[date], now: datetime) -> str:
        span = f"{targets[0]:%Y%m%d}-{targets[-1]:%Y%m%d}" if targets else "none"
        stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")
        return f"ops-{span}-{self.cfg.fingerprint()[:8]}-{stamp}"

    def _result(
        self, status: str, started: str, mode: str, targets: list[str], **kw: Any
    ) -> m.RunResult:
        return m.RunResult(
            run_id=kw.pop("run_id", "-"),
            status=status,
            started_at=started,
            completed_at=self.clock().isoformat(),
            mode=mode,
            target_sessions=targets,
            app_version=__version__,
            git_commit=self.svc.git_commit(),
            config_fingerprint=self.cfg.fingerprint(),
            **kw,
        )


def _precheck_ok(checks: dict[str, dict[str, Any]]) -> bool:
    return all(v.get("ok") or not v.get("critical") for v in checks.values())


def _d(x: date | None) -> str | None:
    return None if x is None else x.isoformat()


def _plan_dict(p: Plan) -> dict[str, Any]:
    d = _d
    return {
        "now": p.now,
        "latest_eligible_session": d(p.latest_eligible),
        "latest_stored_session": d(p.latest_stored),
        "checkpoint": d(p.checkpoint),
        "pending_sessions": [s.isoformat() for s in p.pending],
        "target_sessions": [s.isoformat() for s in p.targets],
        "backlog": p.backlog,
    }
