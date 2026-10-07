"""Run the Phase 9 statistical validation on stored bars.

uv run python -m app.validation.cli run --symbol SPY [--out data/validation]

Prints a summary (formal family, cost sensitivity, redundancy). With --out, writes the full
tables as CSV to a local directory (data/ is git-ignored). Exploratory output only.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from app.backtest.data import load_backtest_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory
from app.validation.config import ValidationConfig
from app.validation.engine import Validator, report_summary

log = get_logger(__name__)
FORMAL_COLUMNS = ("a", "b", "metric", "estimate", "lower", "upper", "p_value", "p_holm", "p_bh")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.validation.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Block-bootstrap validation of the Phase 8 baselines")
    run.add_argument("--symbol", required=True)
    run.add_argument("--out", type=Path, default=None, help="directory for CSV tables")
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
