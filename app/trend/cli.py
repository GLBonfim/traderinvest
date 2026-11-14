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

from app.trend.manifest import (
    AUDIT_PATH,
    MANIFEST_PATH,
    amend_manifest,
    build_manifest,
    manifest_sha256,
    run_audit,
    verify_manifest,
    write_json,
)
from app.trend.sources import FileRecord, fetch_all
from app.trend.universe import load_universe, series_rule_violations

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
    p_audit = sub.add_parser("audit", help="audit a snapshot and freeze the manifest")
    p_audit.add_argument("--snapshot", required=True)
    p_audit.add_argument("--data-end", required=True)
    sub.add_parser("verify", help="re-hash the files of the frozen manifest")
    p_amend = sub.add_parser("amend", help="record registry/rule changes made before any result")
    p_amend.add_argument("--date", required=True)
    p_amend.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    root: Path = args.data_root
    universe = load_universe()

    if args.command == "fetch":
        records = fetch_all(universe, root, args.snapshot)
        path = root / args.snapshot / RECORDS
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
    if args.command == "amend":
        stale = [p for p in verify_manifest(manifest, root, universe) if "universe" not in p]
        if stale:
            print("refused: raw files do not match the manifest", file=sys.stderr)
            for p in stale:
                print(f"  {p}", file=sys.stderr)
            return 2
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


if __name__ == "__main__":
    raise SystemExit(main())
