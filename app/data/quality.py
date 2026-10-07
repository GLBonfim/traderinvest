"""Data-quality issue model. Every problem found in provider data becomes one of these
and is persisted to `data_quality_events` — nothing is fixed or dropped silently."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Severity = Literal["info", "warning", "error", "critical"]

# Actions taken in response to an issue.
EXCLUDED = "excluded"  # bar not persisted
KEPT_FLAGGED = "kept_flagged"  # bar persisted, issue recorded
RECORDED_ONLY = "recorded_only"  # nothing to persist (e.g. missing bar)
DEDUPLICATED = "deduplicated_identical"  # identical duplicates collapsed to one
UPDATED = "updated"  # existing row overwritten with provider's new values
ABORTED = "ingestion_aborted"


@dataclass(frozen=True)
class DataQualityIssue:
    check_name: str
    severity: Severity
    description: str
    action_taken: str
    bar_ts: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)
