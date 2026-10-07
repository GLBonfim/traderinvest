"""Run the Price Action Engine on stored bars and print DESCRIPTIVE counts.

    uv run python -m app.price_action.cli scan --symbol SPY

Counts describe how often structures/events occurred. Outcome counts (failed/held) are
descriptive history, not evidence of any edge.
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
from app.price_action.engine import PriceActionEngine

log = get_logger(__name__)


def _counts(labels: pd.Series) -> dict[str, int]:
    return {str(k): int(v) for k, v in labels.value_counts().sort_index().items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.price_action.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="Describe market structure and price-action events")
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
    a = PriceActionEngine().analyze(bars, instrument_id=instrument_id, timeframe=args.timeframe)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    last = a.state.iloc[-1] if len(a.state) else None
    summary = {
        "symbol": args.symbol.upper(),
        "bars": len(bars),
        "confirmed_swings": len(a.swings),
        "active_zones": len(a.zones),
        "events": _counts(a.events["event_type"] + "_" + a.events["direction"]),
        "outcomes": _counts(a.outcomes["event_type"] + "_" + a.outcomes["outcome"]),
        "structure_bars": {k: int(v) for k, v in a.state["structure"].value_counts().items()},
        "consolidation_bars": int(a.state["in_consolidation"].sum()),
        "latest_state": None
        if last is None
        else {
            "as_of": a.state.index[-1].isoformat(),
            "structure": last["structure"],
            "trend": last["trend"],
            "trend_evidence": last["trend_evidence"],
            "in_consolidation": bool(last["in_consolidation"]),
        },
        "engine_version": a.engine_version,
        "config_fingerprint": a.config_fingerprint,
        "elapsed_ms": elapsed_ms,
        "note": "Descriptive observations only; no trading decision or predictive claim.",
    }
    log.info("price_action.scan", bars=len(bars), elapsed_ms=elapsed_ms)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
