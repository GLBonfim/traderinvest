"""Scan stored bars with the Candlestick Engine and print DESCRIPTIVE counts.

    uv run python -m app.candles.cli scan --symbol SPY

Counts are occurrence frequencies only. They are not evidence of any predictive edge.
"""

import argparse
import json
import sys
import time

from app.candles.engine import CandlestickEngine
from app.candles.loader import load_closed_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.candles.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="Count candlestick observations on stored closed bars")
    scan.add_argument("--symbol", required=True)
    scan.add_argument("--provider", default="yfinance")
    scan.add_argument("--timeframe", default="1d")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    with get_session_factory()() as session:
        instrument_id, bars = load_closed_bars(
            session, args.symbol.upper(), provider=args.provider, timeframe=args.timeframe
        )

    started = time.perf_counter()
    analysis = CandlestickEngine().analyze(
        bars, instrument_id=instrument_id, timeframe=args.timeframe
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    obs = analysis.observations
    summary = {
        "symbol": args.symbol.upper(),
        "provider": args.provider,
        "timeframe": args.timeframe,
        "bars": len(bars),
        "first_bar": bars.index.min().isoformat() if len(bars) else None,
        "last_bar": bars.index.max().isoformat() if len(bars) else None,
        "zero_range_bars": int(analysis.geometry["zero_range"].sum()),
        "invalid_bars": int((~analysis.geometry["valid"]).sum()),
        "observations": len(obs),
        "by_pattern": {k: int(v) for k, v in obs["pattern"].value_counts().sort_index().items()},
        "context_met_by_pattern": {
            k: int(v)
            for k, v in obs[obs["context_requirements_met"].astype(bool)]["pattern"]
            .value_counts()
            .sort_index()
            .items()
        },
        "engine_version": analysis.engine_version,
        "config_fingerprint": analysis.config_fingerprint,
        "elapsed_ms": elapsed_ms,
        "note": "Descriptive occurrence counts only; no predictive claim.",
    }
    log.info("candles.scan", **{k: v for k, v in summary.items() if not isinstance(v, dict)})
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
