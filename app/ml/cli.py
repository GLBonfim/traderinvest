"""Run the Phase 10 ML experiment (research only).

uv run python -m app.ml.cli run --symbol SPY [--out data/ml]

Writes CSV/JSON reports to a git-ignored directory and prints the pre-declared verdict.
"""

import argparse
import json
import sys
from pathlib import Path

from app.backtest.data import load_backtest_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory
from app.ml.engine import MLExperiment

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.ml.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Train on TRAIN, evaluate on VALIDATION/TEST (fixed config)")
    run.add_argument("--symbol", required=True)
    run.add_argument("--out", type=Path, default=Path("data/ml"))
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    with get_session_factory()() as session:
        instrument_id, bars = load_backtest_bars(session, args.symbol.upper())
    report = MLExperiment().run(bars, instrument_id=instrument_id)
    out = report.write(args.out)
    log.info("ml.run", experiment_id=report.experiment_id, verdict=report.overall_verdict)
    print(
        json.dumps(
            {
                "experiment_id": report.experiment_id,
                "git_commit": report.git_commit,
                "output": str(out),
                "sample_counts": report.sample_counts,
                "verdicts": report.verdicts,
                "overall_verdict": report.overall_verdict,
                "timings_ms": report.timings_ms,
                "note": "Research only; not evidence of profitability.",
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
