"""Operational run records (no secrets, no financial logic)."""

from dataclasses import dataclass, field
from typing import Any

# Stages, in execution order (INGEST..ALERTS run per target session).
PRECHECK, INGEST, VALIDATE, PAPER, ALERTS, HEALTH, COMPLETE = (
    "PRECHECK",
    "INGEST",
    "VALIDATE",
    "PAPER",
    "ALERTS",
    "HEALTH",
    "COMPLETE",
)
STAGES = (PRECHECK, INGEST, VALIDATE, PAPER, ALERTS, HEALTH, COMPLETE)

# Stage statuses
OK, SKIPPED, WARNING, FAILED = "ok", "skipped", "warning", "failed"

# Session statuses
SESSION_COMPLETED = "COMPLETED"
SESSION_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
SESSION_WAITING_FOR_DATA = "WAITING_FOR_DATA"
SESSION_DATA_MISSING = "DATA_MISSING"
SESSION_FAILED = "FAILED"

# Run statuses
NO_NEW_COMPLETED_SESSION = "NO_NEW_COMPLETED_SESSION"
NOT_DUE = "NOT_DUE"  # late-data retry interval not elapsed / attempts exhausted (scheduled)
LOCKED = "LOCKED"
PRECHECK_FAILED = "PRECHECK_FAILED"
COMPLETED = "COMPLETED"
COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
WAITING_FOR_DATA = "WAITING_FOR_DATA"
RUN_FAILED = "FAILED"
DRY_RUN = "DRY_RUN"


@dataclass
class StageResult:
    stage: str
    status: str
    started_at: str
    finished_at: str
    session: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def seconds(self) -> float:
        from datetime import datetime

        return (
            datetime.fromisoformat(self.finished_at) - datetime.fromisoformat(self.started_at)
        ).total_seconds()


@dataclass
class SessionResult:
    session: str
    status: str
    stages: list[StageResult] = field(default_factory=list)


@dataclass
class RunResult:
    run_id: str
    status: str
    started_at: str
    completed_at: str
    mode: str  # manual | scheduled | dry_run
    target_sessions: list[str] = field(default_factory=list)
    precheck: StageResult | None = None
    sessions: list[SessionResult] = field(default_factory=list)
    health: StageResult | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)  # dry-run / no-op context
    app_version: str = ""
    git_commit: str = ""
    config_fingerprint: str = ""
