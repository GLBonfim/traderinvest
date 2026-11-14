"""Atomic no-overwrite Windows publication when hard links are unavailable."""
import os
from pathlib import Path

import pytest

from app.risk import artifacts as art

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows rename contract")


def deny_link(*args):
    error = PermissionError(13, "hard links denied")
    error.winerror = 5
    raise error


def test_denied_hardlink_uses_atomic_no_replace_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(art.os, "link", deny_link)
    target = tmp_path / "result.csv"
    art.write_new_atomic(target, "complete")
    assert target.read_text() == "complete"
    assert not target.with_name(target.name + ".tmp").exists()


def test_racing_publication_is_never_overwritten(tmp_path, monkeypatch):
    target = tmp_path / "result.csv"
    def competing_link(*args):
        target.write_text("winner")
        deny_link()
    monkeypatch.setattr(art.os, "link", competing_link)
    with pytest.raises(FileExistsError):
        art.write_new_atomic(target, "loser")
    assert target.read_text() == "winner"
    assert not target.with_name(target.name + ".tmp").exists()


def test_failed_rename_removes_only_own_temporary(tmp_path, monkeypatch):
    monkeypatch.setattr(art.os, "link", deny_link)
    def refuse(*args):
        raise PermissionError("rename denied")
    monkeypatch.setattr(art.os, "rename", refuse)
    with pytest.raises(PermissionError, match="rename denied"):
        art.write_new_atomic(tmp_path / "result.csv", "data")
    assert list(tmp_path.iterdir()) == []


def test_consumed_source_does_not_remove_a_new_foreign_temporary(tmp_path, monkeypatch):
    monkeypatch.setattr(art.os, "link", deny_link)
    rename = art.os.rename
    def publish_then_competitor(src, dst):
        rename(src, dst)
        Path(src).write_text("foreign")
    monkeypatch.setattr(art.os, "rename", publish_then_competitor)
    target = tmp_path / "result.csv"
    art.write_new_atomic(target, "complete")
    assert target.read_text() == "complete"
    assert target.with_name(target.name + ".tmp").read_text() == "foreign"


def test_non_windows_permission_failure_never_uses_replacing_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(art, "_WINDOWS", False, raising=False)
    monkeypatch.setattr(art.os, "link", deny_link)
    def unexpected(*args):
        raise AssertionError("rename must not run on POSIX")
    monkeypatch.setattr(art.os, "rename", unexpected)
    with pytest.raises(PermissionError, match="hard links denied"):
        art.write_new_atomic(tmp_path / "result.csv", "data")
    assert list(tmp_path.iterdir()) == []
