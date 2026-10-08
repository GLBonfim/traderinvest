"""Operations history (git-ignored `data/operations/`), crash-safe like Phases 12 and 14.

    runs.jsonl   append-only, hash-chained events: run_started, stage, session_finished,
                 run_finished (written as they happen, so an interrupted run is visible)
    state.json   versioned checkpoint (atomic replace): last fully completed session,
                 late-data attempt counters, consecutive failures, scheduler heartbeat,
                 operational counters
    pipeline.lock  see lock.py

The checkpoint is only an optimisation of "what is pending": the durable truth lives in the
subsystems themselves (stored bars, paper ledgers, alert store), whose idempotency makes a re-run
of any session after a crash free of duplicate effects.
"""

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from app.alerts.store import _atomic_write, _Chain
from app.operations.models import RunResult, SessionResult, StageResult

STATE_SCHEMA_VERSION = 1


class OpsStoreError(RuntimeError):
    """Operations history is corrupt or incompatible; nothing was modified."""


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_completed_session": None,
        "data_attempts": {},  # session -> {attempts, last_attempt_at, exhausted_at_eligible}
        "consecutive_failures": 0,
        "scheduler": {},
        "last_run": None,
        "counters": {
            "runs": 0,
            "noop_runs": 0,
            "not_due_runs": 0,
            "lock_conflicts": 0,
            "sessions_processed": 0,
            "failed_runs": 0,
            "data_retries": 0,
        },
    }


class OpsStore:
    def __init__(self, root: Path, *, create: bool = True, no_write: bool = False) -> None:
        self.root = Path(root)
        self.no_write = no_write  # dry-run: read existing files, never create or write any
        self.state_path = self.root / "state.json"
        if not self.state_path.exists() and no_write:
            self.state = _empty_state()
        elif not self.state_path.exists():
            if not create:
                raise OpsStoreError(f"no operations store at {self.root}")
            if (self.root / "runs.jsonl").exists():
                raise OpsStoreError("runs.jsonl exists without state.json; refusing")
            self.root.mkdir(parents=True, exist_ok=True)
            self.state = _empty_state()
            self.save()
        else:
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise OpsStoreError("state.json is not valid JSON") from exc
            if self.state.get("schema_version") != STATE_SCHEMA_VERSION:
                raise OpsStoreError("unsupported operations state schema version")
        try:
            self._runs = _Chain(self.root / "runs.jsonl")  # reads only
        except Exception as exc:
            raise OpsStoreError(str(exc)) from exc

    @classmethod
    def read_only(cls, root: Path) -> "OpsStore | None":
        try:
            return cls(root, create=False)
        except OpsStoreError:
            return None

    def reload(self) -> None:
        """Re-read state.json: every mutation is read-modify-write, so two store objects (e.g.
        the scheduler's heartbeat and a pipeline run) never overwrite each other's progress."""
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise OpsStoreError("state.json is not valid JSON") from exc

    def save(self) -> None:
        if self.no_write:
            raise OpsStoreError("operations store opened without write access (dry run)")
        _atomic_write(
            self.state_path, json.dumps(self.state, sort_keys=True, indent=2, default=str)
        )

    # ── checkpoint ──

    @property
    def last_completed_session(self) -> date | None:
        s = self.state["last_completed_session"]
        return None if s is None else date.fromisoformat(s)

    def mark_session_completed(self, session: date) -> None:
        self.reload()
        current = self.last_completed_session
        if current is None or session > current:
            self.state["last_completed_session"] = session.isoformat()
        self.state["data_attempts"].pop(session.isoformat(), None)
        self.state["counters"]["sessions_processed"] += 1
        self.save()

    def data_attempts(self, session: date) -> dict[str, Any]:
        return dict(self.state["data_attempts"].get(session.isoformat(), {"attempts": 0}))

    def record_data_attempt(self, session: date, at: str, exhausted_at: str | None) -> int:
        self.reload()
        rec = self.data_attempts(session)
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["last_attempt_at"] = at
        if exhausted_at:
            rec["exhausted_at_eligible"] = exhausted_at
        self.state["data_attempts"][session.isoformat()] = rec
        if rec["attempts"] > 1:
            self.state["counters"]["data_retries"] += 1
        self.save()
        return int(rec["attempts"])

    def bump(self, counter: str) -> None:
        self.reload()
        self.state["counters"][counter] = int(self.state["counters"].get(counter, 0)) + 1
        self.save()

    def heartbeat(self, info: dict[str, Any]) -> None:
        self.reload()
        self.state["scheduler"] = info
        self.save()

    # ── history (append-only) ──

    def append(self, event: str, run_id: str, **data: Any) -> None:
        if self.no_write:
            raise OpsStoreError("operations store opened without write access (dry run)")
        self._runs.append([{"event": event, "run_id": run_id, **data}])

    def record_stage(self, run_id: str, st: StageResult) -> None:
        self.append("stage", run_id, stage=asdict(st))

    def record_session(self, run_id: str, s: SessionResult) -> None:
        self.append("session_finished", run_id, session=s.session, status=s.status)

    def finish_run(self, r: RunResult, failed: bool) -> None:
        self.reload()
        self.append(
            "run_finished",
            r.run_id,
            status=r.status,
            completed_at=r.completed_at,
            target_sessions=r.target_sessions,
            warnings=r.warnings,
            errors=r.errors,
        )
        c = self.state["counters"]
        c["runs"] += 1
        if failed:
            c["failed_runs"] += 1
            self.state["consecutive_failures"] = int(self.state["consecutive_failures"]) + 1
        else:
            self.state["consecutive_failures"] = 0
        self.state["last_run"] = {
            "run_id": r.run_id,
            "status": r.status,
            "started_at": r.started_at,
            "completed_at": r.completed_at,
            "target_sessions": r.target_sessions,
            "mode": r.mode,
        }
        self.save()

    def events(self) -> list[dict[str, Any]]:
        return list(self._runs.entries)

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Runs newest first, reconstructed from events; a run without `run_finished` is
        reported as INTERRUPTED (e.g. a crash) — its work is re-done idempotently next run."""
        runs: dict[str, dict[str, Any]] = {}
        for e in self._runs.entries:
            r = runs.setdefault(
                e["run_id"],
                {"run_id": e["run_id"], "status": "INTERRUPTED", "stages": [], "sessions": {}},
            )
            if e["event"] == "run_started":
                r.update(
                    started_at=e["started_at"], mode=e["mode"], target_sessions=e["target_sessions"]
                )
            elif e["event"] == "stage":
                r["stages"].append(e["stage"])
            elif e["event"] == "session_finished":
                r["sessions"][e["session"]] = e["status"]
            elif e["event"] == "run_finished":
                r.update(
                    status=e["status"],
                    completed_at=e["completed_at"],
                    warnings=e["warnings"],
                    errors=e["errors"],
                )
        return list(runs.values())[::-1][:limit]

    def metrics(self) -> dict[str, Any]:
        hist = self.history(limit=10**9)
        durations = [
            (s["stage"], (_iso(s["finished_at"]) - _iso(s["started_at"])))
            for r in hist
            for s in r["stages"]
        ]
        by_stage: dict[str, list[float]] = {}
        for stage, d in durations:
            by_stage.setdefault(stage, []).append(d)
        return {
            **self.state["counters"],
            "consecutive_failures": self.state["consecutive_failures"],
            "interrupted_runs": sum(1 for r in hist if r["status"] == "INTERRUPTED"),
            "stage_seconds_mean": {k: round(sum(v) / len(v), 3) for k, v in by_stage.items()},
            "stage_seconds_max": {k: round(max(v), 3) for k, v in by_stage.items()},
        }


def _iso(s: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(s).timestamp()
