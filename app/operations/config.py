"""Operations configuration: transparent operational conventions (never tuned on results)."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"  # git-ignored
OPS_ROOT = DATA_ROOT / "operations"
PAPER_ROOT = DATA_ROOT / "paper" / "accounts"
ALERTS_ROOT = DATA_ROOT / "alerts"


@dataclass(frozen=True)
class OperationsConfig:
    symbol: str = "SPY"
    exchange: str = "XNYS"
    # A session becomes eligible this long after its official (possibly early) close, so the
    # provider can publish the completed daily bar.
    grace_minutes: int = 30
    # At most this many sessions are processed per run (oldest first); the rest wait for the
    # next run. Sessions are never skipped.
    max_catch_up_sessions: int = 10
    # Late/missing provider data: bounded attempts per session, spaced by the interval.
    data_retry_attempts: int = 3
    data_retry_interval_minutes: int = 30
    # Long-running scheduler: how often it wakes up to check whether a run is due.
    scheduler_poll_seconds: int = 300
    # Consecutive failed runs that raise OPS_REPEATED_FAILURE.
    repeated_failure_threshold: int = 3

    def __post_init__(self) -> None:
        for name in (
            "grace_minutes",
            "max_catch_up_sessions",
            "data_retry_attempts",
            "data_retry_interval_minutes",
            "scheduler_poll_seconds",
            "repeated_failure_threshold",
        ):
            if getattr(self, name) < (0 if name == "grace_minutes" else 1):
                raise ValueError(f"{name} out of range")

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
