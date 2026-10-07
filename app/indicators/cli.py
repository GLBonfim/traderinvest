"""Compute indicators on stored bars and print the latest values (descriptive only).

uv run python -m app.indicators.cli latest --symbol SPY
"""

import argparse
import json
import math
import sys
import time

from app.candles.loader import load_closed_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory
from app.indicators.engine import IndicatorEngine

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.indicators.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    latest = sub.add_parser("latest", help="Latest indicator values for a symbol")
    latest.add_argument("--symbol", required=True)
    latest.add_argument("--provider", default="yfinance")
    latest.add_argument("--timeframe", default="1d")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    with get_session_factory()() as session:
        instrument_id, bars = load_closed_bars(
            session, args.symbol.upper(), provider=args.provider, timeframe=args.timeframe
        )

    started = time.perf_counter()
    a = IndicatorEngine().analyze(bars, instrument_id=instrument_id, timeframe=args.timeframe)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    last = a.values.iloc[-1] if len(a.values) else None
    values = (
        {}
        if last is None
        else {
            c: (None if math.isnan(float(last[c])) else round(float(last[c]), 6))
            for c in a.indicator_columns
        }
    )
    summary = {
        "symbol": args.symbol.upper(),
        "bars": len(bars),
        "as_of": None if last is None else a.values.index[-1].isoformat(),
        "values": values,
        "engine_version": a.engine_version,
        "config_fingerprint": a.config_fingerprint,
        "elapsed_ms": elapsed_ms,
        "note": "Indicator values are descriptive features, not trading signals.",
    }
    log.info("indicators.latest", bars=len(bars), elapsed_ms=elapsed_ms)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
