"""Run the Phase 9 statistical validation on stored bars.

uv run python -m app.validation.cli run --symbol SPY [--out data/validation]

Prints a summary (formal family, cost sensitivity, redundancy). With --out, writes the full
tables as CSV to a local directory (data/ is git-ignored). Exploratory output only.

Recorded runs (Phase 5D), in the research run registry (app/research_runs):

uv run python -m app.validation.cli record --symbol SPY --registry data/research_runs
uv run python -m app.validation.cli verify --registry data/research_runs [--run-id ID]

`record` reads the stored bars in a READ ONLY transaction, runs the same Validator with the
same fixed ValidationConfig, and publishes the run (5 CSV tables + summary.json + run.json)
only after every artifact is verified; on error the run is recorded as failed. It runs one job,
synchronously, only when invoked; `--registry` is required (there is no implicit location).
`verify` only reads: it re-checks the history chain, the manifest and every artifact's SHA-256.
Exit codes (record): 0 complete; 1 the run started but did not complete (recorded as failed,
or left incomplete; includes a publication the registry refused, e.g. an existing published run);
2 invalid input (symbol, registry path, bars, parameters or metadata); 3 the registry refused or
is unavailable before anything was published (busy, corrupt history, other registry error, file
system error); 4 data unavailable. verify: 0 complete, 1 not complete / nothing complete,
2 invalid input or unknown run, 3 corrupt history.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.backtest.data import load_backtest_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory
from app.validation.config import ValidationConfig
from app.validation.engine import Validator, report_summary

if TYPE_CHECKING:
    import pandas as pd

    from app.research_runs.registry import Fingerprint, RunRecord

log = get_logger(__name__)
FORMAL_COLUMNS = ("a", "b", "metric", "estimate", "lower", "upper", "p_value", "p_holm", "p_bh")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
EXIT_OK, EXIT_RUN, EXIT_INPUT, EXIT_REGISTRY, EXIT_DATA = 0, 1, 2, 3, 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.validation.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Block-bootstrap validation of the Phase 8 baselines")
    run.add_argument("--symbol", required=True)
    run.add_argument("--out", type=Path, default=None, help="directory for CSV tables")
    rec = sub.add_parser("record", help="Run the validation and record it in a run registry")
    rec.add_argument("--symbol", required=True)
    rec.add_argument("--registry", type=Path, required=True, help="run registry directory")
    ver = sub.add_parser("verify", help="Verify recorded validation runs (read-only)")
    ver.add_argument("--registry", type=Path, required=True, help="run registry directory")
    ver.add_argument("--run-id", default=None, help="run to verify (default: all)")
    args = parser.parse_args(argv)

    if args.command == "record":
        return record(args.symbol, args.registry)
    if args.command == "verify":
        return verify(args.registry, args.run_id)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    with get_session_factory()() as session:
        _, bars = load_backtest_bars(session, args.symbol.upper())

    started = time.perf_counter()
    report = Validator(ValidationConfig()).run(bars)
    elapsed = round(time.perf_counter() - started, 1)
    frames = report.frames()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, frame in frames.items():
            frame.to_csv(args.out / f"{name}.csv", index=False)

    comps = frames["comparisons"]
    formal = comps[comps["family"] == "formal"]
    summary = {
        **report_summary(report),
        "elapsed_s": elapsed,
        "formal_family": formal[list(FORMAL_COLUMNS)].round(6).to_dict("records"),
        "redundancy": frames["redundancy"].round(6).to_dict("records"),
        "note": "Exploratory. Statistical significance is not economic significance; "
        "not evidence of future profitability.",
    }
    log.info("validation.run", elapsed_s=elapsed)
    print(json.dumps(summary, indent=2, default=str))
    return 0


# ── recorded runs (Phase 5D) ──


def _error(message: str, code: int) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _make_validator() -> Validator:
    return Validator(ValidationConfig())


def _load_dataset(symbol: str) -> "tuple[pd.DataFrame, Fingerprint, dict[str, Any]]":
    """Stored bars + dataset identity, read in one READ ONLY transaction (never written)."""
    from sqlalchemy import text

    from app.dashboard.services.market import dataset_identity, load_bars
    from app.research_runs.registry import Fingerprint

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    with get_session_factory()() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            ident = dataset_identity(session, symbol)
            bars = load_bars(session, ident)
        finally:
            session.rollback()
    source = {
        "kind": "local database (read-only)",
        "symbol": ident.symbol,
        "instrument_id": ident.instrument_id,
        "provider": ident.provider,
        "timeframe": ident.timeframe,
        "bars": ident.bars,
        "first_ts": ident.first_ts.isoformat(),
        "last_ts": ident.last_ts.isoformat(),
        "last_ingested_at": ident.last_ingested_at.isoformat(),
    }
    return bars, Fingerprint.known(ident.key), source


def _record_dict(r: "RunRecord") -> dict[str, Any]:
    return {
        "run_id": r.run_id,
        "kind": r.kind,
        "status": r.status.value,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "failure": r.failure,
        "problems": list(r.problems),
        "artifacts": (
            [{"path": a.path, "bytes": a.bytes, "sha256": a.sha256} for a in r.manifest.artifacts]
            if r.manifest
            else None
        ),
    }


def record(symbol: str, registry_dir: Path) -> int:
    from app.dashboard.services.market import NoDataError
    from app.research_runs.registry import (
        ManifestError,
        RegistryBusyError,
        RegistryCorruptError,
        RegistryError,
        RunRegistry,
    )
    from app.research_runs.validation import ValidationRunError, record_validation

    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        return _error(f"invalid symbol {symbol!r}", EXIT_INPUT)
    if registry_dir.exists() and not registry_dir.is_dir():
        return _error(f"registry path is not a directory: {registry_dir}", EXIT_INPUT)
    try:
        bars, dataset_key, source = _load_dataset(symbol)
    except NoDataError as exc:
        return _error(f"data unavailable: {exc}", EXIT_DATA)
    except Exception as exc:  # e.g. database unreachable; the message may carry connection details
        return _error(f"data unavailable: could not read the local database ({type(exc).__name__})",
                      EXIT_DATA)  # fmt: skip

    registry = RunRegistry(registry_dir)
    started = time.perf_counter()
    try:
        rec = record_validation(
            bars, registry=registry, validator=_make_validator(), dataset_key=dataset_key,
            source=source,
        )  # fmt: skip
    except ValidationRunError as exc:
        status = exc.record.status.value if exc.record else "incomplete"
        return _error(f"validation run {exc.run_id} {status}: {exc}", EXIT_RUN)
    except (ManifestError, ValueError, TypeError) as exc:
        return _error(f"invalid input: {exc}", EXIT_INPUT)
    except RegistryBusyError as exc:
        return _error(f"registry busy (nothing published): {exc}", EXIT_REGISTRY)
    except RegistryCorruptError as exc:
        return _error(f"registry history is corrupt (nothing published): {exc}", EXIT_REGISTRY)
    except RegistryError as exc:
        return _error(f"registry error (nothing published): {exc}", EXIT_REGISTRY)
    except OSError as exc:
        return _error(
            f"registry unavailable (nothing published): {type(exc).__name__}: {exc}", EXIT_REGISTRY
        )
    elapsed = round(time.perf_counter() - started, 1)
    summary = json.loads(
        (registry.published / rec.run_id / "summary.json").read_text(encoding="utf-8")
    )
    out = {
        **_record_dict(rec),
        "elapsed_s": elapsed,
        "run_dir": str(registry.published / rec.run_id),
        "formal_family_size": summary["formal_family_size"],
        "formal_holm_below_0_05": summary["formal_holm_below_0_05"],
        "formal_bh_below_0_05": summary["formal_bh_below_0_05"],
        "verify": f"python -m app.validation.cli verify --registry {registry_dir} "
        f"--run-id {rec.run_id}",
        "note": summary["note"],
    }
    print(json.dumps(out, indent=2))
    return EXIT_OK


def verify(registry_dir: Path, run_id: str | None) -> int:
    from app.research_runs.registry import RegistryCorruptError, RunRegistry, RunStatus

    if registry_dir.exists() and not registry_dir.is_dir():
        return _error(f"registry path is not a directory: {registry_dir}", EXIT_INPUT)
    registry = RunRegistry(registry_dir)  # reading never creates or modifies anything
    try:
        if run_id is not None:
            try:
                rec = registry.get(run_id)
            except KeyError:
                return _error(f"unknown run {run_id!r}", EXIT_INPUT)
            if rec.kind != "validation":
                return _error(f"run {run_id} is not a validation run", EXIT_INPUT)
            print(json.dumps(_record_dict(rec), indent=2))
            return EXIT_OK if rec.status is RunStatus.COMPLETE else EXIT_RUN
        records = [r for r in registry.records() if r.kind == "validation"]
        latest = registry.latest_complete("validation")
        unregistered = registry.unregistered()
    except RegistryCorruptError as exc:
        return _error(f"registry history is corrupt: {exc}", EXIT_REGISTRY)
    print(
        json.dumps(
            {
                "runs": [
                    {"run_id": r.run_id, "status": r.status.value, "problems": list(r.problems)}
                    for r in records
                ],
                "latest_complete": latest.run_id if latest else None,
                "unregistered": unregistered,
            },
            indent=2,
        )
    )
    return EXIT_OK if latest is not None else EXIT_RUN


if __name__ == "__main__":
    sys.exit(main())
