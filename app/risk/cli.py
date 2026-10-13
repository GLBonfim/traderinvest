"""Run the pre-declared risk scenarios over the Phase 7 baselines (measurement only).

uv run python -m app.risk.cli run --symbol SPY [--out-root data/risk | --out DIR]
uv run python -m app.risk.cli run --symbol SPY --strategy sma_trend \
    --scenario control_no_overlay fixed_fraction_50 --cost D --slice full [--no-compare]

Without selection options the whole suite runs (every strategy x scenario x slice, cost D),
as before. With ANY of --strategy/--scenario/--cost/--slice, all four are required and only
the selected cases run (see `app.risk.research`). Every run writes into a NEW directory
(default `<out-root>/<run_id>`; an existing path is refused) with `manifest.json`,
`risk_table.csv` and, with comparisons, `risk_comparisons.csv` (git-ignored data/). Not an
optimisation: no scenario is selected.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import get_logger
from app.risk import research
from app.risk.diagnostics import run_risk_suite

log = get_logger(__name__)

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_OUTPUT_EXISTS = 0, 1, 2, 3


def _db_loader(symbol: str) -> research.BarsLoader:
    """Loads the bars through a verified read-only connection (`app.database.readonly`): a
    dedicated engine whose every transaction is BEGIN READ ONLY, each statement preceded by a
    confirmed `SHOW transaction_read_only = on`. Any failed check aborts before data is read."""

    def load() -> research.LoadResult:
        from app.backtest.data import load_backtest_bars
        from app.database.readonly import ReadOnlyEvidence, read_only_engine, read_only_session

        engine = read_only_engine()
        evidence = ReadOnlyEvidence()
        try:
            with read_only_session(engine, evidence) as (session, _):
                _, bars = load_backtest_bars(session, symbol)
        except Exception as exc:
            raise research.LoadFailedError(
                f"{type(exc).__name__}: {exc}", evidence.to_dict()
            ) from exc
        finally:
            engine.dispose()
        return research.LoadResult(bars, evidence.to_dict())

    return load


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.risk.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Risk overlays on the baselines (control always included)")
    run.add_argument("--symbol", required=True)
    out = run.add_mutually_exclusive_group()
    out.add_argument("--out", type=Path, help="exact NEW run directory (refused if it exists)")
    out.add_argument(
        "--out-root", type=Path, default=Path("data/risk"), help="parent of <run_id> directories"
    )
    run.add_argument("--strategy", nargs="+", metavar="ID",
                     help=f"one or more of: {', '.join(research.strategy_ids())}")  # fmt: skip
    run.add_argument("--scenario", nargs="+", metavar="NAME",
                     help=f"risk scenarios (must include {research.CONTROL}): "
                     f"{', '.join(research.scenario_names())}")  # fmt: skip
    run.add_argument("--cost", metavar="NAME",
                     help=f"one cost scenario: {', '.join(research.cost_names())}")  # fmt: skip
    run.add_argument("--slice", nargs="+", metavar="NAME",
                     help=f"one or more of: {', '.join(research.slice_names())}")  # fmt: skip
    run.add_argument("--no-compare", action="store_true",
                     help="selective runs only: skip the bootstrap comparisons")  # fmt: skip
    return parser


def main(
    argv: list[str] | None = None,
    *,
    load_bars: research.BarsLoader | None = None,
    run_suite: research.SuiteRunner | None = None,
    configure: bool = True,
) -> int:
    args = build_parser().parse_args(argv)
    try:  # 1. validate the selection: nothing is read or written before this succeeds
        sel = research.resolve_selection(
            strategies=args.strategy,
            scenarios=args.scenario,
            cost=args.cost,
            slices=args.slice,
            compare=not args.no_compare,
        )
    except research.SelectionError as exc:
        print(f"app.risk.cli: error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if configure:
        from app.core.config import get_settings
        from app.core.logging import configure_logging

        settings = get_settings()
        configure_logging(settings.log_level, settings.log_format)

    # 2. a new, exclusive output directory (atomic mkdir; existing paths are never touched)
    run_id = research.new_run_id(datetime.now(UTC))
    run_dir: Path = args.out if args.out is not None else args.out_root / run_id
    try:
        if args.out is None:
            args.out_root.mkdir(parents=True, exist_ok=True)  # the parent only
        research.create_run_dir(run_dir)
    except FileExistsError as exc:
        print(f"app.risk.cli: error: {exc}; nothing was written", file=sys.stderr)
        return EXIT_OUTPUT_EXISTS
    except OSError as exc:
        print(f"app.risk.cli: error: cannot create {run_dir}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    symbol = args.symbol.upper()
    log.info("risk.run_started", run_id=run_id, selection=sel.to_dict(), output=str(run_dir))
    outcome = research.execute(
        sel,
        symbol=symbol,
        run_dir=run_dir,
        run_id=run_id,
        load_bars=load_bars or _db_loader(symbol),
        run_suite=run_suite or run_risk_suite,
        command=["app.risk.cli", *(argv if argv is not None else sys.argv[1:])],
    )
    log.info("risk.run_finished", run_id=run_id, status=outcome.status)
    m = outcome.manifest
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": outcome.status,
                "output": str(run_dir),
                "selection": m["selection"],
                "data_fingerprint": (m.get("input") or {}).get("fingerprint", {}).get("value"),
                "artifacts": [a["path"] for a in m["artifacts"]],
                "error": outcome.error,
                "note": "Measurement only; lower risk usually means lower return.",
            },
            indent=2,
        )
    )
    return EXIT_OK if outcome.status == research.COMPLETE else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
