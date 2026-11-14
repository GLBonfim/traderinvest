"""Permanent dataset-wide M5 holdout claims, independent of run directories.

Exclusive creation is the concurrency boundary. An empty/partial claim after a
crash still consumes the holdout: there is deliberately no release or reset API.
This cannot detect historical consumption or an administrator copying/deleting
an entire dataset; those actions require separate provenance governance.
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "m5-holdout-consumption/2"
BOUNDARY_SCHEMA = "m5-holdout-boundary/1"
BOUNDARY_NAME = ".m5-holdout-boundary.json"
CLAIM_NAME = ".m5-holdout-consumed.json"


def _reject_consumed(root: Path) -> None:
    # lexists deliberately includes a dangling symlink; no receipt is ever read
    # to decide whether a consumption entry is valid enough to block access.
    path = root / CLAIM_NAME
    if os.path.lexists(path):
        raise ValueError(f"M5 holdout already consumed or claimed: {path}")


def prepare_holdout(data: Path, holdout_start: str) -> Path:
    """Freeze the dataset boundary before ANY development read; never widen it.

    Exclusive creation permits at most one initial boundary. A concurrent caller
    seeing an unpublished creator claim refuses before its boundary is
    established. Repeated development runs with the same committed boundary are
    allowed until consumption. Empty/corrupt/dangling entries fail closed.
    """
    root = data.resolve()
    _reject_consumed(root)
    stamp = datetime.fromisoformat(holdout_start)
    if stamp.tzinfo is None:
        raise ValueError("holdout boundary needs an explicit timezone")
    cutoff = stamp.astimezone(UTC).isoformat()
    root.mkdir(parents=True, exist_ok=True)
    path = root / BOUNDARY_NAME
    expected = {
        "schema": BOUNDARY_SCHEMA,
        "dataset_root": str(root),
        "timeframe": "M5",
        "holdout_start": cutoff,
    }
    if os.path.lexists(path):
        try:
            if path.is_symlink():
                raise ValueError("holdout boundary must not be a symlink")
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid or incomplete holdout boundary: {path}") from exc
        if stored != expected:
            raise ValueError(f"incompatible frozen holdout boundary: {path}")
    else:
        # Keep this exclusive creator claim permanently. A crash before publication
        # leaves it behind and refuses all later callers; no reset/release exists.
        creator = root / (BOUNDARY_NAME + ".claim")
        try:
            stream = creator.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise ValueError(f"holdout boundary publication incomplete: {creator}") from exc
        with stream:
            json.dump(expected, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        # Retain the creator claim permanently, and publish a separate staged
        # copy only after fsync. No second legitimate writer can ever acquire
        # that claim, including a caller that raced the first existence check.
        pending = root / (BOUNDARY_NAME + ".pending")
        with pending.open("x", encoding="utf-8") as staged:
            json.dump(expected, staged, indent=2, allow_nan=False)
            staged.flush()
            os.fsync(staged.fileno())
        pending.rename(path)  # same-directory atomic publication; Windows refuses replacement
    _reject_consumed(root)
    return path


def claim_holdout(
    data: Path,
    out: Path,
    *,
    protocol_sha: str,
    selection_sha: str,
    holdout_start: str,
    pairs: tuple[str, ...],
) -> Path:
    """Consume once, after frozen selection and before any unrestricted load.

    One claim covers this entire M5 dataset, including changed split dates,
    manifests, pair subsets and protocols. Those fields never create a new key.
    Atomic exclusive creation is effective across processes on the same local
    filesystem. Any write/fsync error leaves the claim in place and propagates.
    """
    root = data.resolve()
    _reject_consumed(root)
    selection = out.resolve() / "selection.json"
    if not selection.is_file():
        raise ValueError("holdout requires frozen selection before consumption")
    try:
        frozen = json.loads(selection.read_text(encoding="utf-8"))
        actual_sha = hashlib.sha256(
            json.dumps(frozen, sort_keys=True, allow_nan=False, default=str).encode()
        ).hexdigest()
    except (OSError, ValueError) as exc:
        raise ValueError("holdout selection hash cannot be verified") from exc
    if actual_sha != selection_sha:
        raise ValueError("holdout selection hash differs from frozen selection")
    boundary = prepare_holdout(root, holdout_start)
    path = root / CLAIM_NAME
    receipt = {
        "schema": SCHEMA,
        "state": "consumed",
        "dataset_root": str(root),
        "timeframe": "M5",
        "boundary_path": str(boundary),
        "claimed_at_utc": datetime.now(UTC).isoformat(),
        "protocol_sha256": protocol_sha,
        "selection_sha256": selection_sha,
        "selection_path": str(selection),
        "output_root": str(out.resolve()),
        "holdout_start": holdout_start,
        "pairs": sorted(pairs),
        "policy": "Permanent claim; errors and crashes never release or rearm access.",
    }
    try:
        stream = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError(f"M5 holdout already consumed or claimed: {path}") from exc
    with stream:
        json.dump(receipt, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    return path
