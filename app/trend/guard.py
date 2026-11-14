"""Sealed-test guard for the trend research (design v2 section 10).

Same pattern as `app/fx/research/holdout_guard.py`, re-implemented here:

- `prepare_boundary` freezes the sealed start and the manifest hash in a boundary file made by
  exclusive creation, before any development read. A later call must match it exactly.
- `claim_sealed` consumes the sealed test once: it creates the claim file exclusively and
  records the protocol hash, code commit, manifest hash and reference capital. There is no
  reset or release API. An empty or partial claim left by a crash still counts as consumed.
- `check_claim_for_manifest` is what the data loader calls before it reads sealed rows.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.trend.manifest import manifest_sha256

BOUNDARY_SCHEMA = "trend-sealed-boundary/1"
CLAIM_SCHEMA = "trend-sealed-claim/1"
BOUNDARY_NAME = ".trend-sealed-boundary.json"
CLAIM_NAME = ".trend-sealed-claim.json"
GUARD_ROOT = Path("data/trend/guard")


class GuardError(ValueError):
    """The sealed test cannot be prepared, claimed or read."""


def _boundary_record(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": BOUNDARY_SCHEMA,
        "sealed_start": manifest["partitions"]["sealed"]["start"],
        "manifest_sha256": manifest_sha256(manifest),
    }


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:  # raises FileExistsError if present
        json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def prepare_boundary(root: Path, manifest: dict[str, Any]) -> Path:
    """Create the boundary once; afterwards only an identical boundary is accepted."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / BOUNDARY_NAME
    expected = _boundary_record(manifest)
    if os.path.lexists(path):
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GuardError(f"invalid sealed boundary {path}") from exc
        if stored != expected:
            raise GuardError("sealed boundary differs (sealed start or manifest hash changed)")
        return path
    try:
        _write_exclusive(path, expected)
    except FileExistsError as exc:
        raise GuardError("sealed boundary is being created by another process") from exc
    return path


def claim_sealed(
    root: Path,
    manifest: dict[str, Any],
    *,
    protocol_sha256: str,
    code_commit: str,
    reference_capital_eur: float,
) -> dict[str, Any]:
    """Consume the sealed test (once, forever). Returns the receipt."""
    boundary = root / BOUNDARY_NAME
    if not boundary.is_file():
        raise GuardError("no sealed boundary: prepare it before any development run")
    try:
        stored = json.loads(boundary.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardError(f"invalid sealed boundary {boundary}") from exc
    if stored != _boundary_record(manifest):
        raise GuardError("manifest hash or sealed start differs from the frozen boundary")
    path = root / CLAIM_NAME
    if os.path.lexists(path):
        raise GuardError(f"sealed test already consumed or claimed: {path}")
    receipt = {
        "schema": CLAIM_SCHEMA,
        "state": "consumed",
        "claimed_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "sealed_start": stored["sealed_start"],
        "manifest_sha256": stored["manifest_sha256"],
        "protocol_sha256": protocol_sha256,
        "code_commit": code_commit,
        "reference_capital_eur": reference_capital_eur,
    }
    try:
        _write_exclusive(path, receipt)
    except FileExistsError as exc:
        raise GuardError(f"sealed test already consumed or claimed: {path}") from exc
    return receipt


def read_claim(root: Path) -> dict[str, Any]:
    path = root / CLAIM_NAME
    if not os.path.lexists(path):
        raise GuardError("the sealed test has not been claimed")
    try:
        claim: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardError("the sealed claim is incomplete (consumed by a crash)") from exc
    return claim


def check_claim_for_manifest(claim: dict[str, Any] | None, manifest: dict[str, Any]) -> None:
    """Refuse sealed rows unless a consumed claim for this exact manifest is presented."""
    if claim is None:
        raise GuardError("sealed rows need a sealed claim")
    if claim.get("schema") != CLAIM_SCHEMA or claim.get("state") != "consumed":
        raise GuardError("not a valid sealed claim")
    if claim.get("manifest_sha256") != manifest_sha256(manifest):
        raise GuardError("the sealed claim was made for a different manifest")
