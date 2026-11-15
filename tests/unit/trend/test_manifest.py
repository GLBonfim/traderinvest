import json
from pathlib import Path
from typing import Any

import pytest

from app.core.integrity import atomic_write, canonical_json
from app.trend.manifest import (
    DATA_FIELDS,
    SCHEMA,
    amend_manifest,
    cost_table,
    manifest_sha256,
    reaudit_amendment,
    verify_manifest,
)
from app.trend.sources import FileRecord, needed_roles, sha256_file
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


def _full_manifest(tmp_path: Path) -> dict[str, Any]:
    m = _manifest(tmp_path)
    return m | {
        "snapshot": "snap",
        "data_end": "2026-11-13",
        "window": {"evaluable_start": "2009-10-08"},
        "partitions": {"sealed": {"start": "2023-06-01"}},
        "audit_verdict": "PASS",
        "audit_sha256": "a" * 64,
        "parameters": {"sma_sessions": 200},
        "universe_sha256": "0" * 64,
    }


def test_amendment_keeps_data_and_chains_hash(tmp_path: Path) -> None:
    old = _full_manifest(tmp_path)
    u = load_universe()
    new = amend_manifest(old, u, "2026-11-14", "owner decisions")
    for key in DATA_FIELDS:
        assert new.get(key) == old.get(key)
    assert new["universe_sha256"] == u.sha256
    assert new["amendments"] == [
        {"date": "2026-11-14", "previous_sha256": manifest_sha256(old), "reason": "owner decisions"}
    ]
    again = amend_manifest(new, u, "2026-11-15", "second")
    assert [a["previous_sha256"] for a in again["amendments"]] == [
        manifest_sha256(old),
        manifest_sha256(new),
    ]
    assert verify_manifest(again, tmp_path, u) == []


def test_cost_table_lists_every_series_tier() -> None:
    u = load_universe()
    table = cost_table(u)
    assert set(table["asset_tiers"]) == {a.id for a in u.all_series()}
    assert table["asset_tiers"]["GLOBAL_RE"]["cost_class"] == "C"
    assert table["asset_tiers"]["US_EQ"]["ter_proxy_pct"] == 0.0945
    assert table["asset_tiers"]["EUROPE_EQ"]["ter_proxy_pct"] is None
    assert table["commission"]["confirmed_by_owner_on"] == "2026-11-14"


def _rec(role: str, sha: str) -> FileRecord:
    return FileRecord(role, f"snap/{role}.csv", sha, 1, "2026-11-14T00:00:00+00:00", "test")


def test_reaudit_amendment_adds_drops_and_protects_kept_files() -> None:
    u = load_universe()
    roles = needed_roles(u)
    old_files = [_rec(r, "1" * 64).to_dict() for r in roles[2:]] + [
        _rec("price:OLD", "2" * 64).to_dict()
    ]
    previous = {
        "schema": SCHEMA,
        "data_end": "2026-11-13",
        "files": old_files,
        "window": {"evaluable_start": "2009-10-08"},
        "partitions": {"p": 1},
        "parameters": {"sma_sessions": 200},
    }
    report = {
        "verdict": "PASS",
        "data_end": "2026-11-13",
        "window": {"evaluable_start": "2009-10-08", "x": 1},
        "partitions": {"p": 1},
    }
    records = [_rec(r, "1" * 64) for r in roles]
    new = reaudit_amendment(previous, u, records, report, "2026-11-14", "swap")
    entry = new["amendments"][-1]
    assert entry["files_added"] == sorted(roles[:2])
    assert entry["files_dropped"] == ["price:OLD"]
    assert entry["changed_fields"] == ["window", "audit_sha256"]
    assert {f["role"] for f in new["files"]} == set(roles)

    tampered = [_rec(roles[-1], "f" * 64), *[_rec(r, "1" * 64) for r in roles[:-1]]]
    with pytest.raises(ValueError, match="changed its hash"):
        reaudit_amendment(previous, u, tampered, report, "2026-11-14", "swap")
    with pytest.raises(ValueError, match="data end"):
        reaudit_amendment(previous, u, records, report | {"data_end": "2026-11-20"}, "d", "r")
    with pytest.raises(ValueError, match="did not pass"):
        reaudit_amendment(previous, u, records, report | {"verdict": "FAIL"}, "d", "r")
