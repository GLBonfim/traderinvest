"""Shared integrity primitives moved from app.alerts.store: identical algorithms and on-disk
format, and the old names keep working for the alert, broker and operations stores."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.alerts import store as alert_store
from app.alerts.store import AlertStoreError
from app.core.integrity import ChainError, HashChain, atomic_write, canonical_json, entry_hash


def _old_canonical(obj: Any) -> str:  # the pre-move implementation, verbatim
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _old_hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(
        _old_canonical({k: v for k, v in entry.items() if k != "hash"}).encode()
    ).hexdigest()


SAMPLES: list[Any] = [
    {"b": 2, "a": [1, 2.5, None], "c": {"z": "é", "y": True}},
    {"seq": 1, "x": "2026-10-09T13:49:37.271934+00:00", "prev_hash": ""},
]


@pytest.mark.parametrize("obj", SAMPLES)
def test_canonical_json_and_hash_are_unchanged(obj: Any) -> None:
    assert canonical_json(obj) == _old_canonical(obj)
    assert entry_hash(obj) == _old_hash(obj)


def test_nan_is_still_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_alert_store_names_are_aliases_of_the_shared_primitives() -> None:
    assert alert_store._canonical is canonical_json
    assert alert_store._hash is entry_hash
    assert alert_store._atomic_write is atomic_write
    assert issubclass(alert_store._Chain, HashChain)
    assert alert_store._Chain.error is AlertStoreError and HashChain.error is ChainError


def _old_format_file(path: Path, bodies: list[dict[str, Any]]) -> None:
    """Writes a chain exactly as the pre-move code did."""
    prev, lines = "", []
    for n, b in enumerate(bodies, start=1):
        e = {"seq": n, **b, "prev_hash": prev}
        e["hash"] = _old_hash(e)
        lines.append(_old_canonical(e) + "\n")
        prev = e["hash"]
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


@pytest.mark.parametrize("cls", [HashChain, alert_store._Chain])
def test_files_written_by_the_old_code_are_read_and_extended(
    tmp_path: Path, cls: type[HashChain]
) -> None:
    p = tmp_path / "chain.jsonl"
    _old_format_file(p, [{"k": 1}, {"k": 2}])
    chain = cls(p)
    assert [e["k"] for e in chain.entries] == [1, 2]
    chain.append([{"k": 3}])
    reread = cls(p)
    assert [e["seq"] for e in reread.entries] == [1, 2, 3]
    expected = tmp_path / "expected.jsonl"
    _old_format_file(expected, [{"k": 1}, {"k": 2}, {"k": 3}])
    assert p.read_bytes() == expected.read_bytes()  # byte-identical to the old format


@pytest.mark.parametrize(
    ("cls", "error"), [(HashChain, ChainError), (alert_store._Chain, AlertStoreError)]
)
def test_tampering_raises_the_class_specific_error(
    tmp_path: Path, cls: type[HashChain], error: type[Exception]
) -> None:
    p = tmp_path / "chain.jsonl"
    _old_format_file(p, [{"k": 1}, {"k": 2}])
    p.write_text(
        p.read_text(encoding="utf-8").replace('"k":2', '"k":9'), encoding="utf-8", newline="\n"
    )
    with pytest.raises(error, match="line 2: hash chain broken"):
        cls(p)
    p.write_text("not json\n", encoding="utf-8")
    with pytest.raises(error, match="line 1 is not valid JSON"):
        cls(p)


def test_atomic_write_replaces_and_leaves_no_temporary(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    atomic_write(p, "first")
    atomic_write(p, "second\n")
    assert p.read_bytes() == b"second\n"
    assert not (tmp_path / "state.json.tmp").exists()


def test_atomic_write_failure_keeps_the_previous_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "state.json"
    atomic_write(p, "good")

    def boom(src: Any, dst: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(p, "bad")
    monkeypatch.undo()
    assert p.read_text(encoding="utf-8") == "good"
