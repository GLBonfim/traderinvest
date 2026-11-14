"""Owner control file: only a valid file changes the aula02 veto, always with an audit record."""

import hashlib
import json
from pathlib import Path

from app.fx.ibkr.lab_control import FILENAME, SCHEMA, lesson_h1_setting


def write(root: Path, data: object) -> bytes:
    raw = json.dumps(data).encode("utf-8")
    (root / FILENAME).write_bytes(raw)
    return raw


def test_missing_file_keeps_the_command_line_value(tmp_path: Path) -> None:
    assert lesson_h1_setting(tmp_path, True) == (True, None)
    assert lesson_h1_setting(tmp_path, False) == (False, None)


def test_valid_file_turns_the_veto_off_and_is_audited(tmp_path: Path) -> None:
    raw = write(
        tmp_path,
        {
            "schema": SCHEMA,
            "lesson_h1": False,
            "reason": "dados novos",
            "requested_by": "dono",
            "requested_at": "2026-10-28T16:00:00Z",
        },
    )
    effective, audit = lesson_h1_setting(tmp_path, True)
    assert effective is False
    assert audit is not None and audit["applied"] is True
    assert audit["sha256"] == hashlib.sha256(raw).hexdigest()
    assert audit["reason"] == "dados novos" and audit["lesson_h1_requested"] is True


def test_file_agreeing_with_the_command_line_is_recorded_but_not_applied(tmp_path: Path) -> None:
    write(tmp_path, {"schema": SCHEMA, "lesson_h1": True})
    effective, audit = lesson_h1_setting(tmp_path, True)
    assert effective is True and audit is not None and audit["applied"] is False


def test_invalid_files_never_change_anything(tmp_path: Path) -> None:
    for bad in (
        {"schema": "other/1", "lesson_h1": False},
        {"schema": SCHEMA, "lesson_h1": "no"},
        [1],
    ):
        write(tmp_path, bad)
        effective, audit = lesson_h1_setting(tmp_path, True)
        assert effective is True and audit is not None and audit["applied"] is False
    (tmp_path / FILENAME).write_text("{not json", encoding="utf-8")
    effective, audit = lesson_h1_setting(tmp_path, True)
    assert effective is True and audit is not None and "invalid" in audit["error"]
