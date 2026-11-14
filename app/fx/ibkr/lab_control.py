"""Owner control file for the paper candle lab: `<lab dir>/lab-control.json`.

Lets the owner change a lab setting without editing the supervisor that launches the lab:
the file is read once, when each lab process starts, so it takes effect on the next restart
(the supervisor restarts the lab after a STOP is removed, reconciling cash first).

Schema `fx-lab-control/1`:

    {"schema": "fx-lab-control/1", "lesson_h1": false,
     "reason": "...", "requested_by": "...", "requested_at": "<ISO>"}

Only `lesson_h1` (the aula02 H4/H1 veto) is controlled today. A missing or invalid file never
changes anything: the command-line value stays. The applied decision is returned with the
file's SHA-256 so the lab can journal it.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "fx-lab-control/1"
FILENAME = "lab-control.json"


def lesson_h1_setting(root: Path, requested: bool) -> tuple[bool, dict[str, Any] | None]:
    """(effective lesson_h1, audit record or None when the command line value stands)."""
    path = root / FILENAME
    if not path.is_file():
        return requested, None
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return requested, {"applied": False, "error": "invalid JSON", "file": str(path)}
    if not isinstance(data, dict):
        return requested, {"applied": False, "error": "invalid schema/value", "file": str(path)}
    value = data.get("lesson_h1")
    if data.get("schema") != SCHEMA or not isinstance(value, bool):
        return requested, {"applied": False, "error": "invalid schema/value", "file": str(path)}
    return value, {
        "applied": value != requested,
        "lesson_h1_requested": requested,
        "lesson_h1_effective": value,
        "reason": str(data.get("reason", "")),
        "requested_by": str(data.get("requested_by", "")),
        "requested_at": str(data.get("requested_at", "")),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
