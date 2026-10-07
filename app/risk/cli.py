"""Run the pre-declared risk scenarios over the Phase 7 baselines (measurement only).

uv run python -m app.risk.cli run --symbol SPY [--out data/risk]

Writes the per-(slice, strategy, scenario) table and the descriptive overlay-vs-control
comparisons as CSV (git-ignored data/). Not an optimisation: no scenario is selected.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from app.backtest.data import load_backtest_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory
from app.risk.diagnostics import run_risk_suite

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.risk.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Risk overlays on the baselines (control always included)")
    run.add_argument("--symbol", required=True)
    run.add_argument("--out", type=Path, default=Path("data/risk"))
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    with get_session_factory()() as session:
        _, bars = load_backtest_bars(session, args.symbol.upper())
    started = time.perf_counter()
    report = run_risk_suite(bars)
    elapsed = round(time.perf_counter() - started, 1)
    args.out.mkdir(parents=True, exist_ok=True)
    report.table().to_csv(args.out / "risk_table.csv", index=False)
    pd.DataFrame(report.comparisons).to_csv(args.out / "risk_comparisons.csv", index=False)
    log.info("risk.run", elapsed_s=elapsed)
    print(
        json.dumps(
            {
                "risk_version": report.risk_version,
                "scenarios": report.scenarios,
                "backtest_fingerprint": report.backtest_fingerprint,
                "runs": len(report.runs),
                "descriptive_comparisons": len(report.comparisons),
                "output": str(args.out),
                "elapsed_s": elapsed,
                "timings_ms": report.timings_ms,
                "note": "Measurement only; lower risk usually means lower return.",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
