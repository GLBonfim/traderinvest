"""Run the six baseline strategies and both buy & hold benchmarks through the backtester.

uv run python -m app.backtest.cli baseline --symbol SPY [--start YYYY-MM-DD] [--end YYYY-MM-DD]

Descriptive diagnostics under explicit assumptions; not evidence of future profitability.
"""

import argparse
import json
import math
import sys
import time
from dataclasses import asdict
from datetime import date

from app.backtest.config import BacktestConfig
from app.backtest.data import load_backtest_bars
from app.backtest.engine import run_suite
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory

log = get_logger(__name__)


def _clean(value: float) -> float | None:
    return None if isinstance(value, float) and math.isnan(value) else round(value, 6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.backtest.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    base = sub.add_parser("baseline", help="Backtest the Phase 7 baselines (diagnostic)")
    base.add_argument("--symbol", required=True)
    base.add_argument("--start", type=date.fromisoformat, default=None)
    base.add_argument("--end", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    with get_session_factory()() as session:
        instrument_id, bars = load_backtest_bars(session, args.symbol.upper())

    config = BacktestConfig(start=args.start, end=args.end)
    started = time.perf_counter()
    results = run_suite(bars, config=config, instrument_id=instrument_id)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    summary = {
        "symbol": args.symbol.upper(),
        "assumptions": {k: str(v) for k, v in asdict(config).items()},
        "results": {
            sid: {k: _clean(v) for k, v in r.metrics.items()} for sid, r in results.items()
        },
        "elapsed_ms": elapsed_ms,
        "note": "Historical simulation under stated assumptions; not evidence of profitability.",
    }
    log.info("backtest.baseline", bars=len(bars), elapsed_ms=elapsed_ms)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
