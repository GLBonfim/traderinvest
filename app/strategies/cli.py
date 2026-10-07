"""Run the baseline strategies on stored bars and print state counts and the latest states.

uv run python -m app.strategies.cli run --symbol SPY

Prints states and timestamps only. No returns, no performance: that is Phase 8.
"""

import argparse
import json
import sys
import time

import pandas as pd

from app.candles.loader import load_closed_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory
from app.strategies.engine import StrategyEngine

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.strategies.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Baseline states for a symbol (no performance)")
    run.add_argument("--symbol", required=True)
    run.add_argument("--provider", default="yfinance")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    with get_session_factory()() as session:
        instrument_id, bars = load_closed_bars(session, args.symbol.upper(), provider=args.provider)

    started = time.perf_counter()
    result = StrategyEngine().run(bars, instrument_id=instrument_id)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    latest = result.signals.groupby("strategy_id", sort=False).tail(1)
    summary = {
        "symbol": args.symbol.upper(),
        "bars": len(bars),
        "strategies": result.summary.to_dict("records"),
        "latest": [
            {
                "strategy_id": str(r["strategy_id"]),
                "state": str(r["state"]),
                "observed_at": pd.Timestamp(r["observed_at"]).isoformat(),
                "effective_at": pd.Timestamp(r["effective_at"]).isoformat(),
            }
            for r in latest.to_dict("records")
        ],
        "engine_version": result.engine_version,
        "config_fingerprint": result.config_fingerprint,
        "elapsed_ms": elapsed_ms,
        "note": "Baseline strategies are research benchmarks, not evidence of profitability.",
    }
    log.info("strategies.run", bars=len(bars), elapsed_ms=elapsed_ms)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
