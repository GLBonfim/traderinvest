"""Sealed-test guard for the trend research (design v2 section 10).

Same pattern as `app/fx/research/holdout_guard.py`, re-implemented here:

- `prepare_boundary` freezes the sealed start and the manifest hash in a boundary file made by
  exclusive creation, before any development read. A later call must match it exactly.
- `claim_sealed` consumes the sealed test once: it creates the claim file exclusively and
  records the protocol hash, code commit, manifest hash and reference capital. There is no
  reset or release API. An empty or partial claim left by a crash still counts as consumed.
- `check_claim_for_manifest` checks that a claim is consumed and made for this manifest.

Publication order (owner decision 2026-11-14, design section 19):

- T3 start: `publish_boundary` creates the boundary and copies it to
  ``app/trend/registry/sealed_boundary.json``, which is committed to Git before the first
  development run.
- T4: (1) `claim_sealed` creates the claim and copies the receipt (protocol hash, code commit,
  manifest hash, reference capital, UTC date and time) to
  ``app/trend/registry/sealed_claim.json``; (2) the receipt is committed and pushed; (3) only
  then may sealed rows be read. `require_published_claim`, called by the data loader, refuses
  unless the registry receipt and the receipt on the upstream branch (``git show @{u}:path``)
  both equal the claim.
"""

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.trend.manifest import manifest_sha256
from app.trend.universe import REGISTRY_DIR

BOUNDARY_SCHEMA = "trend-sealed-boundary/1"
CLAIM_SCHEMA = "trend-sealed-claim/1"
BOUNDARY_NAME = ".trend-sealed-boundary.json"
CLAIM_NAME = ".trend-sealed-claim.json"
GUARD_ROOT = Path("data/trend/guard")
PUBLISHED_BOUNDARY = REGISTRY_DIR / "sealed_boundary.json"
PUBLISHED_CLAIM = REGISTRY_DIR / "sealed_claim.json"
PUBLISHED_CLAIM_GIT_PATH = "app/trend/registry/sealed_claim.json"


class GuardError(ValueError):
    """The sealed test cannot be prepared, claimed or read."""


def _boundary_record(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": BOUNDARY_SCHEMA,
        "sealed_start": manifest["partitions"]["sealed"]["start"],
        "manifest_sha256": manifest_sha256(manifest),
    }


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:  # FileExistsError if present
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
    publish_to: Path | None = PUBLISHED_CLAIM,
) -> dict[str, Any]:
    """Consume the sealed test (once, forever): step (1). Returns the receipt."""
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
    if publish_to is not None:
        _write_exclusive(publish_to, receipt)  # committed and pushed before any sealed read
    return receipt


def publish_boundary(
    root: Path, manifest: dict[str, Any], publish_to: Path = PUBLISHED_BOUNDARY
) -> dict[str, Any]:
    """Prepare the boundary and copy it to the registry (committed to Git at T3 start)."""
    path = prepare_boundary(root, manifest)
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if publish_to.exists():
        if json.loads(publish_to.read_text(encoding="utf-8")) != record:
            raise GuardError("published boundary differs from the frozen boundary")
    else:
        _write_exclusive(publish_to, record)
    return record


def git_upstream_receipt() -> dict[str, Any] | None:
    """The receipt as committed on the upstream branch, or None if it is not there."""
    try:
        text = subprocess.run(  # noqa: S603 - fixed arguments, no user input
            ["git", "show", "@{u}:" + PUBLISHED_CLAIM_GIT_PATH],  # noqa: S607 - git on PATH
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    receipt: dict[str, Any] = json.loads(text)
    return receipt


def require_published_claim(
    claim: dict[str, Any] | None,
    manifest: dict[str, Any],
    published: Path = PUBLISHED_CLAIM,
    upstream: Callable[[], dict[str, Any] | None] = git_upstream_receipt,
) -> None:
    """Step (3) gate: a consumed claim for this manifest whose receipt is committed and pushed."""
    check_claim_for_manifest(claim, manifest)
    if not published.is_file():
        raise GuardError("the claim receipt is not in the registry; publish it first")
    if json.loads(published.read_text(encoding="utf-8")) != claim:
        raise GuardError("the registry receipt differs from the claim")
    if upstream() != claim:
        raise GuardError("the claim receipt is not committed and pushed to the upstream branch")


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
