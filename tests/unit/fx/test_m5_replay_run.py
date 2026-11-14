import hashlib
import json
from pathlib import Path

import pytest

from app.fx.research.m5_replay_run import frozen_model


def write_model(root, training_end):
    raw = {"training_end": training_end, "offline_only": True, "execution_enabled": False}
    path = root / "models"
    path.mkdir()
    (path / "impulso-3-fold-2025.json").write_text(json.dumps(raw), encoding="utf-8")
    h = hashlib.sha256(
        json.dumps(raw, sort_keys=True, allow_nan=False, default=str).encode()
    ).hexdigest()
    (root / "candidates.json").write_text(
        json.dumps([{"candidate": "impulso-3", "folds": [{"fold": "2025", "model_sha256": h}]}]),
        encoding="utf-8",
    )
    return raw


def test_model_temporal_and_hash_contract(tmp_path):
    raw = write_model(tmp_path, "2025-01-01T00:00:00+00:00")
    assert frozen_model(tmp_path, "impulso-3", "2025", "2025-01-01T00:00:00+00:00") == raw
    p = tmp_path / "models/impulso-3-fold-2025.json"
    p.write_text(
        p.read_text(encoding="utf-8").replace("2025-01-01", "2024-01-01"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="hash"):
        frozen_model(tmp_path, "impulso-3", "2025", "2025-01-01T00:00:00+00:00")


def test_future_training_refused(tmp_path):
    write_model(tmp_path, "2026-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="training"):
        frozen_model(tmp_path, "impulso-3", "2025", "2025-01-01T00:00:00+00:00")


def test_replay_chunk_verification_skips_future_files(tmp_path):
    from app.fx.research.m5_replay_run import verify_replay_chunks

    pair = tmp_path / "EURUSD"
    pair.mkdir()
    (pair / "old.parquet").write_bytes(b"synthetic")
    old = {
        "pair": "EURUSD",
        "file": "old.parquet",
        "first": "2025-01-01T00:00:00Z",
        "last": "2025-01-31T00:00:00Z",
        "sha256": hashlib.sha256(b"synthetic").hexdigest(),
    }
    future = {**old, "file": "missing-future.parquet", "first": "2026-01-01T00:00:00Z"}
    items = verify_replay_chunks(
        tmp_path, {"chunks": [old, future]}, "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"
    )
    assert len(items) == 1
    (pair / "old.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="chunk hash"):
        verify_replay_chunks(
            tmp_path, {"chunks": [old]}, "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"
        )


def test_loader_reads_only_verified_file_list(monkeypatch, tmp_path):
    import pandas as pd

    from app.fx.research.m5_replay_run import load_replay_frame

    idx = pd.date_range("2025-01-01", periods=6, freq="5min", tz="UTC")
    reads = []

    def read(path, **kwargs):
        reads.append(Path(path).name)
        assert Path(path).name in ("BID-good.parquet", "ASK-good.parquet")
        px = 1.1 if Path(path).name.startswith("BID") else 1.1001
        return pd.DataFrame(
            {"ts": idx, "open": px, "high": px + 0.001, "low": px - 0.001, "close": px}
        )

    monkeypatch.setattr(pd, "read_parquet", read)
    chunks = [
        {"pair": "EURUSD", "side": side, "file": side + "-good.parquet"} for side in ("BID", "ASK")
    ]
    out = load_replay_frame(tmp_path, "EURUSD", chunks, idx[0], idx[-1] + pd.Timedelta(minutes=5))
    assert len(out) == 6 and len(reads) == 2


def test_depth5_model_selection_preserves_frozen_evidence(tmp_path):
    from app.fx.research.m5_replay_run import select_models

    raw = write_model(tmp_path, "2025-01-01T00:00:00+00:00")
    old = tmp_path / "models/impulso-3-fold-2025.json"
    for role in ("reversao", "continuacao", "impulso"):
        (old.parent / f"{role}-5-fold-2025.json").write_bytes(old.read_bytes())
    evidence = json.loads((tmp_path / "candidates.json").read_text(encoding="utf-8"))[0]["folds"]
    (tmp_path / "candidates.json").write_text(json.dumps([
        {"candidate": role + "-5", "folds": evidence}
        for role in ("reversao", "continuacao", "impulso")
    ]), encoding="utf-8")
    selected = select_models(tmp_path, 5, "2025-01-01T00:00:00+00:00")
    assert len(selected) == 3 and all(m == raw for m in selected.values())
    with pytest.raises(ValueError, match="depth"):
        select_models(tmp_path, 4, "2025-01-01T00:00:00+00:00")
