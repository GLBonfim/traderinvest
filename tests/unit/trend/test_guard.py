import json
from pathlib import Path
from typing import Any

import pytest

from app.trend.data import build_market
from app.trend.guard import (
    CLAIM_NAME,
    GuardError,
    check_claim_for_manifest,
    claim_sealed,
    prepare_boundary,
    publish_boundary,
    read_claim,
    require_published_claim,
)
from app.trend.manifest import MANIFEST_PATH
from app.trend.universe import load_universe


def _manifest(sealed: str = "2023-06-01", tag: str = "a") -> dict[str, Any]:
    return {"partitions": {"sealed": {"start": sealed}}, "tag": tag}


def _claim(root: Path, manifest: dict[str, Any], publish: Path | None = None) -> dict[str, Any]:
    return claim_sealed(
        root,
        manifest,
        protocol_sha256="p" * 64,
        code_commit="abc1234",
        reference_capital_eur=5000,
        publish_to=publish,
    )


def test_claim_refuses_without_boundary(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match="no sealed boundary"):
        _claim(tmp_path, _manifest())


def test_single_permanent_claim(tmp_path: Path) -> None:
    m = _manifest()
    prepare_boundary(tmp_path, m)
    prepare_boundary(tmp_path, m)  # idempotent with the same manifest
    receipt = _claim(tmp_path, m)
    assert receipt["state"] == "consumed" and receipt["reference_capital_eur"] == 5000
    assert read_claim(tmp_path) == receipt
    with pytest.raises(GuardError, match="already consumed"):
        _claim(tmp_path, m)


def test_refuses_when_hashes_differ(tmp_path: Path) -> None:
    prepare_boundary(tmp_path, _manifest())
    with pytest.raises(GuardError, match="differs"):
        prepare_boundary(tmp_path, _manifest(tag="b"))
    with pytest.raises(GuardError, match="differs"):
        prepare_boundary(tmp_path, _manifest(sealed="2023-07-01"))
    with pytest.raises(GuardError, match="differs"):
        _claim(tmp_path, _manifest(tag="b"))


def test_crash_after_claim_still_consumes(tmp_path: Path) -> None:
    m = _manifest()
    prepare_boundary(tmp_path, m)
    (tmp_path / CLAIM_NAME).write_text("", encoding="utf-8")  # crash before the receipt
    with pytest.raises(GuardError, match="already consumed"):
        _claim(tmp_path, m)
    with pytest.raises(GuardError, match="incomplete"):
        read_claim(tmp_path)


def test_claim_must_match_the_manifest(tmp_path: Path) -> None:
    m = _manifest()
    prepare_boundary(tmp_path, m)
    receipt = _claim(tmp_path, m)
    check_claim_for_manifest(receipt, m)
    with pytest.raises(GuardError):
        check_claim_for_manifest(None, m)
    with pytest.raises(GuardError, match="different manifest"):
        check_claim_for_manifest(receipt, _manifest(tag="b"))
    with pytest.raises(GuardError):
        check_claim_for_manifest(receipt | {"state": "open"}, m)


def test_loader_refuses_sealed_rows_without_a_claim(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    sealed = manifest["partitions"]["sealed"]["start"]
    with pytest.raises(GuardError):
        build_market(manifest, load_universe(), tmp_path, through=sealed)
    with pytest.raises(GuardError):
        build_market(
            manifest,
            load_universe(),
            tmp_path,
            through=manifest["data_end"],
            sealed_claim={"schema": "trend-sealed-claim/1", "state": "consumed"},
        )


def test_boundary_is_published_once_and_must_match(tmp_path: Path) -> None:
    out = tmp_path / "sealed_boundary.json"
    record = publish_boundary(tmp_path / "guard", _manifest(), out)
    assert json.loads(out.read_text(encoding="utf-8")) == record
    assert publish_boundary(tmp_path / "guard", _manifest(), out) == record  # idempotent
    with pytest.raises(GuardError):
        publish_boundary(tmp_path / "guard", _manifest(tag="b"), out)


def test_sealed_reads_need_the_receipt_committed_and_pushed(tmp_path: Path) -> None:
    m = _manifest()
    guard, registry = tmp_path / "guard", tmp_path / "sealed_claim.json"
    prepare_boundary(guard, m)
    receipt = _claim(guard, m, publish=registry)  # step 1: claim and registry copy
    assert receipt["claimed_at_utc"] and receipt["code_commit"] == "abc1234"
    assert json.loads(registry.read_text(encoding="utf-8")) == receipt
    with pytest.raises(GuardError, match="not committed and pushed"):
        require_published_claim(receipt, m, registry, upstream=lambda: None)  # step 2 missing
    with pytest.raises(GuardError, match="not committed and pushed"):
        require_published_claim(receipt, m, registry, upstream=lambda: receipt | {"x": 1})
    require_published_claim(receipt, m, registry, upstream=lambda: receipt)  # step 3 allowed
    with pytest.raises(GuardError, match="not in the registry"):
        require_published_claim(receipt, m, tmp_path / "absent.json", upstream=lambda: receipt)
    with pytest.raises(GuardError, match="different manifest"):
        require_published_claim(receipt, _manifest(tag="b"), registry, upstream=lambda: receipt)
