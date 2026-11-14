import json
from pathlib import Path
from typing import Any

from app.core.integrity import atomic_write, canonical_json
from app.trend.manifest import SCHEMA, manifest_sha256, verify_manifest
from app.trend.sources import sha256_file
from app.trend.universe import load_universe


def _manifest(tmp_path: Path) -> dict[str, Any]:
    raw = tmp_path / "snap" / "yahoo" / "X.csv"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"date,close\n2024-01-02,1\n")
    return {
        "schema": SCHEMA,
        "universe_sha256": load_universe().sha256,
        "files": [{"role": "price:X", "path": "snap/yahoo/X.csv", "sha256": sha256_file(raw)}],
    }


def test_verify_accepts_untouched_files(tmp_path: Path) -> None:
    assert verify_manifest(_manifest(tmp_path), tmp_path, load_universe()) == []


def test_verify_detects_changed_and_missing_files(tmp_path: Path) -> None:
    m = _manifest(tmp_path)
    raw = tmp_path / "snap" / "yahoo" / "X.csv"
    raw.write_bytes(b"date,close\n2024-01-02,2\n")
    assert verify_manifest(m, tmp_path, load_universe()) == ["hash mismatch snap/yahoo/X.csv"]
    raw.unlink()
    assert verify_manifest(m, tmp_path, load_universe()) == ["missing raw file snap/yahoo/X.csv"]


def test_verify_detects_changed_universe(tmp_path: Path) -> None:
    m = _manifest(tmp_path) | {"universe_sha256": "0" * 64}
    assert "universe registry changed" in verify_manifest(m, tmp_path, load_universe())[0]


def test_manifest_hash_ignores_key_order() -> None:
    assert manifest_sha256({"a": 1, "b": [1, 2]}) == manifest_sha256({"b": [1, 2], "a": 1})


def test_reused_integrity_primitives(tmp_path: Path) -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    target = tmp_path / "out.json"
    atomic_write(target, json.dumps({"x": 1}))
    assert json.loads(target.read_text(encoding="utf-8")) == {"x": 1}
    assert not (tmp_path / "out.json.tmp").exists()
