"""Permanent holdout claims; synthetic directories and data only."""

import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from app.fx.research import m5_validation as m5
from tests.unit.fx.test_m5_validation import _loader, _tiny_protocol, m5_frame


def claim(root, out):
    from app.fx.research.holdout_guard import claim_holdout

    out.mkdir(parents=True, exist_ok=True)
    (out / "selection.json").write_text("{}")
    return claim_holdout(
        root,
        out,
        protocol_sha="a" * 64,
        selection_sha=m5.digest({}),
        holdout_start="2024-04-01T00:00:00+00:00",
        pairs=("EURUSD",),
    )


def test_different_output_cannot_reconsume_and_receipt_is_provenance(tmp_path):
    root = tmp_path / "synthetic-dataset"
    receipt = claim(root, tmp_path / "run-one")
    record = json.loads(receipt.read_text())
    assert record["state"] == "consumed"
    assert record["dataset_root"] == str(root.resolve())
    assert record["selection_sha256"] == m5.digest({})
    with pytest.raises(ValueError, match="holdout"):
        claim(root, tmp_path / "run-two")


def test_concurrent_claims_have_exactly_one_winner(tmp_path):
    root = tmp_path / "synthetic-dataset"

    def attempt(i):
        try:
            claim(root, tmp_path / f"run-{i}")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(8))) == 1


def test_abandoned_claim_fails_closed_and_protocol_changes_do_not_rearm(tmp_path):
    receipt = claim(tmp_path / "synthetic-dataset", tmp_path / "run-one")
    receipt.write_text("")
    from app.fx.research.holdout_guard import claim_holdout

    second = tmp_path / "run-two"
    second.mkdir()
    (second / "selection.json").write_text("{}")
    with pytest.raises(ValueError, match="already consumed or claimed"):
        claim_holdout(
            tmp_path / "synthetic-dataset",
            second,
            protocol_sha="changed",
            selection_sha=m5.digest({}),
            holdout_start="2025-01-01T00:00:00+00:00",
            pairs=("EURJPY",),
        )


def test_runner_claims_before_unrestricted_loader_and_keeps_claim_on_crash(tmp_path):
    root, out = tmp_path / "synthetic-dataset", tmp_path / "run-one"
    lax = _tiny_protocol(
        gate_min_closed=0,
        gate_min_positive_periods=0,
        gate_min_profit_factor=0.0,
        gate_min_mean_r=-1e9,
        gate_min_stress_mean_r=-1e9,
        min_pattern_strength=0.0,
        require_model_agreement=False,
    )
    delegate = _loader(out, [])

    def loader(data, symbol, before):
        if before is None:
            assert (out / "selection.json").exists()
            assert (root / ".m5-holdout-consumed.json").exists()
            raise RuntimeError("synthetic crash after claim")
        return delegate(data, symbol, before)

    with pytest.raises(RuntimeError, match="synthetic crash"):
        m5.run(root, out, lax, loader, require_manifest=False, say=lambda _: None)
    with pytest.raises(ValueError, match="holdout"):
        claim(root, tmp_path / "run-two")


def test_cutoff_short_chunk_uses_combined_cadence(tmp_path):
    df = m5_frame(days=2)
    directory = tmp_path / "EURUSD"
    directory.mkdir()
    for side in ("BID", "ASK"):
        part = pd.DataFrame(
            {k: df[f"{side.lower()}_{k}"] for k in ("open", "high", "low", "close")}
        ).reset_index(names="ts")
        part.iloc[:300].to_parquet(directory / f"{side}_M5_20240305.parquet")
        part.iloc[300:].to_parquet(directory / f"{side}_M5_20240306.parquet")
    cutoff = df.index[301]
    assert len(m5.load_m5(tmp_path, "EURUSD", cutoff)) == 301


@pytest.mark.parametrize("content", ["", "broken-json"])
def test_consumed_changed_cutoff_refuses_before_any_loader(tmp_path, content):
    root = tmp_path / "synthetic-dataset"
    root.mkdir()
    (root / ".m5-holdout-consumed.json").write_text(content)
    calls = []

    def sentinel(*args):
        calls.append(args)
        pytest.fail("loader reached consumed holdout")

    with pytest.raises(ValueError, match="already consumed or claimed"):
        m5.run(
            root,
            tmp_path / "new-output",
            _tiny_protocol(holdout_start="2027-01-01T00:00:00+00:00"),
            sentinel,
            require_manifest=False,
            say=lambda _: None,
        )
    assert calls == []


def test_dangling_consumption_entry_refuses_before_loader(tmp_path, monkeypatch):
    import os

    root = tmp_path / "synthetic-dataset"
    marker = root / ".m5-holdout-consumed.json"
    original = os.path.lexists
    # Simulate a dangling directory entry without requiring Windows symlink privileges.
    monkeypatch.setattr(os.path, "lexists", lambda p: str(p) == str(marker) or original(p))
    calls = []

    def sentinel(*args):
        calls.append(args)
        pytest.fail("loader reached dangling claim")

    with pytest.raises(ValueError, match="already consumed or claimed"):
        m5.run(
            root,
            tmp_path / "new-output",
            _tiny_protocol(),
            sentinel,
            require_manifest=False,
            say=lambda _: None,
        )
    assert calls == []


def test_boundary_frozen_before_first_loader_and_cannot_widen(tmp_path):
    root = tmp_path / "synthetic-dataset"

    def first_loader(*args):
        assert (root / ".m5-holdout-boundary.json").is_file()
        raise RuntimeError("stop after preflight")

    with pytest.raises(RuntimeError, match="stop after preflight"):
        m5.run(
            root,
            tmp_path / "one",
            _tiny_protocol(),
            first_loader,
            require_manifest=False,
            say=lambda _: None,
        )
    calls = []

    def later_loader(*args):
        calls.append(args)
        pytest.fail("incompatible cutoff reached loader")

    with pytest.raises(ValueError, match="boundary"):
        m5.run(
            root,
            tmp_path / "two",
            _tiny_protocol(holdout_start="2027-01-01T00:00:00+00:00"),
            later_loader,
            require_manifest=False,
            say=lambda _: None,
        )
    assert calls == []


def test_concurrent_incompatible_boundaries_have_one_winner(tmp_path):
    from app.fx.research.holdout_guard import prepare_holdout

    root = tmp_path / "synthetic-dataset"

    def attempt(cutoff):
        try:
            prepare_holdout(root, cutoff)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert (
            sum(pool.map(attempt, ["2024-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"])) == 1
        )


def test_claim_checks_actual_canonical_selection_hash(tmp_path):
    from app.fx.research.holdout_guard import claim_holdout

    root, out = tmp_path / "synthetic-dataset", tmp_path / "one"
    out.mkdir()
    (out / "selection.json").write_text('{"changed": true}')
    with pytest.raises(ValueError, match=r"selection.*hash"):
        claim_holdout(
            root,
            out,
            protocol_sha="a" * 64,
            selection_sha=m5.digest({}),
            holdout_start="2024-04-01T00:00:00+00:00",
            pairs=("EURUSD",),
        )
    assert not (root / ".m5-holdout-consumed.json").exists()


@pytest.mark.parametrize("content", ["", "broken-json"])
def test_incomplete_boundary_refuses_before_any_loader(tmp_path, content):
    root = tmp_path / "synthetic-dataset"
    root.mkdir()
    (root / ".m5-holdout-boundary.json").write_text(content)
    calls = []

    def sentinel(*args):
        calls.append(args)
        pytest.fail("loader reached incomplete boundary")

    with pytest.raises(ValueError, match="boundary"):
        m5.run(
            root,
            tmp_path / "one",
            _tiny_protocol(),
            sentinel,
            require_manifest=False,
            say=lambda _: None,
        )
    assert calls == []


def test_uncommitted_boundary_is_not_visible_to_concurrent_reader(tmp_path, monkeypatch):
    import os
    from threading import Event

    from app.fx.research.holdout_guard import prepare_holdout

    reached_fsync, release_fsync = Event(), Event()
    original = os.fsync

    def delayed_fsync(fd):
        reached_fsync.set()
        assert release_fsync.wait(timeout=5)
        return original(fd)

    monkeypatch.setattr(os, "fsync", delayed_fsync)
    root = tmp_path / "synthetic-dataset"
    cutoff = "2024-04-01T00:00:00+00:00"
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(prepare_holdout, root, cutoff)
        try:
            assert reached_fsync.wait(timeout=5)
            with pytest.raises(ValueError, match="boundary"):
                prepare_holdout(root, cutoff)
        finally:
            release_fsync.set()
        assert writer.result().is_file()
    assert prepare_holdout(root, cutoff).is_file()


def test_first_real_loader_run_cannot_move_reserved_default_boundary(tmp_path, monkeypatch):
    calls = []

    def sentinel(*args):
        calls.append(args)
        pytest.fail("real loader reached reserved holdout")

    monkeypatch.setattr(m5, "load_m5", sentinel)
    root = tmp_path / "synthetic-dataset"
    with pytest.raises(ValueError, match="reserved"):
        m5.run(
            root,
            tmp_path / "one",
            _tiny_protocol(holdout_start="2027-01-01T00:00:00+00:00"),
            loader=m5.load_m5,
            require_manifest=False,
            say=lambda _: None,
        )
    assert calls == []
    assert not (root / ".m5-holdout-boundary.json").exists()
