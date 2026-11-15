"""Command line for the trend research (offline only).

    uv run python -m app.trend.cli fetch  --snapshot 2026-11-14
    uv run python -m app.trend.cli audit  --snapshot 2026-11-14 --data-end 2026-11-13
    uv run python -m app.trend.cli verify
    uv run python -m app.trend.cli amend --date 2026-11-14 --reason "..."

`audit` writes ``registry/audit.json`` and freezes ``registry/manifest.json``; `audit` never
overwrites an existing manifest. `amend` records registry or rule changes made before any
result: it keeps the data part, refreshes the universe hash, rules and cost table, and chains
the previous manifest hash. `verify` also lists series-rule violations (owner condition a),
which block the replay.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.trend.guard import PUBLISHED_BOUNDARY
from app.trend.manifest import (
    AUDIT_PATH,
    MANIFEST_PATH,
    amend_manifest,
    build_manifest,
    manifest_sha256,
    reaudit_amendment,
    run_audit,
    verify_manifest,
    write_json,
)
from app.trend.sources import FileRecord, fetch_all, fetch_missing
from app.trend.universe import Universe, load_universe, series_rule_violations

DATA_ROOT = Path("data/trend/raw")
RECORDS = "records.json"


def _records(root: Path, snapshot: str) -> list[FileRecord]:
    raw = json.loads((root / snapshot / RECORDS).read_text(encoding="utf-8"))
    return [FileRecord(**r) for r in raw]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.trend.cli")
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    p_fetch = sub.add_parser("fetch", help="download a new raw snapshot")
    p_fetch.add_argument("--snapshot", required=True)
    p_fetch.add_argument(
        "--add-missing",
        action="store_true",
        help="add only the series a changed universe needs to an existing snapshot",
    )
    p_audit = sub.add_parser("audit", help="audit a snapshot and freeze the manifest")
    p_audit.add_argument("--snapshot", required=True)
    p_audit.add_argument("--data-end", required=True)
    sub.add_parser("verify", help="re-hash the files of the frozen manifest")
    p_amend = sub.add_parser("amend", help="record registry/rule changes made before any result")
    p_amend.add_argument("--date", required=True)
    p_amend.add_argument("--reason", required=True)
    p_amend.add_argument("--reaudit-snapshot", help="re-run the audit after new files were added")
    p_t3 = sub.add_parser("t3", help="development and validation replays (sealed test closed)")
    p_t3.add_argument("--out", type=Path, default=Path("data/trend/results/t3"))
    p_t3.add_argument("--report-dir", type=Path, default=Path("docs/trend/t3"))
    args = parser.parse_args(argv)
    root: Path = args.data_root
    universe = load_universe()

    if args.command == "fetch":
        path = root / args.snapshot / RECORDS
        if args.add_missing:
            existing = _records(root, args.snapshot)
            added = fetch_missing(universe, root, args.snapshot, existing)
            records = existing + added
            path.write_text(json.dumps([r.to_dict() for r in records], indent=1), encoding="utf-8")
            print(f"{len(added)} file(s) added: {[r.role for r in added]}")
            return 0
        records = fetch_all(universe, root, args.snapshot)
        path.write_text(json.dumps([r.to_dict() for r in records], indent=1), encoding="utf-8")
        print(f"{len(records)} files written to {root / args.snapshot}")
        return 0

    if args.command == "audit":
        if MANIFEST_PATH.exists():
            print(f"refused: manifest already frozen at {MANIFEST_PATH}", file=sys.stderr)
            return 2
        records = _records(root, args.snapshot)
        report = run_audit(universe, records, root, args.data_end)
        write_json(AUDIT_PATH, report)
        if report["verdict"] != "PASS":
            print("audit FAILED:\n  " + "\n  ".join(report["failures"]), file=sys.stderr)
            return 1
        manifest = build_manifest(universe, records, report, args.snapshot)
        write_json(MANIFEST_PATH, manifest)
        print(f"audit PASS; manifest {manifest_sha256(manifest)}")
        return 0

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if args.command == "t3":
        return _t3(manifest, universe, root, args.out, args.report_dir)
    if args.command == "amend":
        if PUBLISHED_BOUNDARY.exists():
            print("refused: the sealed boundary exists, results may exist", file=sys.stderr)
            return 2
        stale = [p for p in verify_manifest(manifest, root, universe) if "universe" not in p]
        if stale:
            print("refused: raw files do not match the manifest", file=sys.stderr)
            for p in stale:
                print(f"  {p}", file=sys.stderr)
            return 2
        if args.reaudit_snapshot:
            if args.reaudit_snapshot != manifest["snapshot"]:
                print("refused: re-audit must use the manifest's snapshot", file=sys.stderr)
                return 2
            records = _records(root, args.reaudit_snapshot)
            report = run_audit(universe, records, root, manifest["data_end"])
            if report["verdict"] != "PASS":
                print("audit FAILED:\n  " + "\n  ".join(report["failures"]), file=sys.stderr)
                return 1
            amended = reaudit_amendment(manifest, universe, records, report, args.date, args.reason)
            from app.trend.data import unit_anchors

            amended["unit_anchors"] = unit_anchors(amended, universe, root)
            write_json(AUDIT_PATH, report)
        else:
            amended = amend_manifest(manifest, universe, args.date, args.reason)
        write_json(MANIFEST_PATH, amended)
        print(f"manifest amended: {manifest_sha256(manifest)} -> {manifest_sha256(amended)}")
        return 0

    problems = verify_manifest(manifest, root, universe)
    for p in problems:
        print(p, file=sys.stderr)
    print(f"manifest {manifest_sha256(manifest)}")
    print("hashes OK" if not problems else f"{len(problems)} problem(s)")
    violations = series_rule_violations(universe)
    for v in violations:
        print(f"series rule (a) violated, blocks the replay: {v}")
    return 0 if not problems and not violations else 1


def _code_state() -> dict[str, str]:
    """HEAD commit and the SHA-256 of the uncommitted diff of the trend code (provenance)."""
    import hashlib
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed git arguments
            ["git", *args],  # noqa: S607 - git on PATH
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    diff = git("diff", "HEAD", "--", "app/trend", "tests/unit/trend")
    untracked = git("ls-files", "--others", "--exclude-standard", "app/trend", "tests/unit/trend")
    blob = diff + "".join(
        Path(f).read_text(encoding="utf-8") for f in sorted(untracked.split()) if f.endswith(".py")
    )
    return {
        "head": git("rev-parse", "HEAD").strip(),
        "uncommitted_sha256": hashlib.sha256(blob.encode()).hexdigest(),
        "uncommitted_files": ",".join(sorted(untracked.split())),
    }


def _t3(manifest: dict[str, Any], universe: Universe, root: Path, out: Path, docs: Path) -> int:
    import csv

    from app.trend.guard import GUARD_ROOT, prepare_boundary
    from app.trend.research import json_ready, run_t3

    problems = verify_manifest(manifest, root, universe) + series_rule_violations(universe)
    if problems:
        print("refused: " + "; ".join(problems), file=sys.stderr)
        return 2
    prepare_boundary(GUARD_ROOT, manifest)  # must equal the boundary committed at T3 start
    results = run_t3(manifest, universe, root)
    out.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)
    payload = json_ready(results)
    payload["manifest_sha256"] = manifest_sha256(manifest)
    payload["code"] = _code_state()
    write_json(out / "results.json", payload)
    for part in payload["partitions"]:
        for capital, rows in part.pop("trades").items():
            target = out / f"trades_{part['partition']}_{capital}.csv"
            with target.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["none"])
                writer.writeheader()
                writer.writerows(rows)
    write_json(docs / "summary.json", payload)
    print(f"T3 done; reference capital {payload['minimum_viable_capital']['reference_capital']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
