"""Describe the market regime of stored bars (descriptive only).

uv run python -m app.regimes.cli describe --symbol SPY
"""

import argparse
import json
import sys
import time

from app.candles.loader import load_closed_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_session_factory
from app.regimes.engine import DIMENSIONS, RegimeEngine

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.regimes.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    describe = sub.add_parser("describe", help="Latest regime state and label counts")
    describe.add_argument("--symbol", required=True)
    describe.add_argument("--provider", default="yfinance")
    describe.add_argument("--timeframe", default="1d")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    with get_session_factory()() as session:
        instrument_id, bars = load_closed_bars(
            session, args.symbol.upper(), provider=args.provider, timeframe=args.timeframe
        )

    started = time.perf_counter()
    a = RegimeEngine().analyze(bars, instrument_id=instrument_id, timeframe=args.timeframe)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    st = a.state
    last = st.iloc[-1] if len(st) else None
    summary = {
        "symbol": args.symbol.upper(),
        "bars": len(bars),
        "as_of": None if last is None else st.index[-1].isoformat(),
        "latest": None
        if last is None
        else {
            dim: {"regime": last[f"{dim}_regime"], "age_bars": int(last[f"{dim}_age"])}
            for dim in DIMENSIONS
        },
        "label_counts": {
            dim: {str(k): int(v) for k, v in st[f"{dim}_regime"].value_counts().items()}
            for dim in DIMENSIONS
        },
        "engine_version": a.engine_version,
        "config_fingerprint": a.config_fingerprint,
        "elapsed_ms": elapsed_ms,
        "note": "Regime labels describe observed conditions; not signals or predictions.",
    }
    log.info("regimes.describe", bars=len(bars), elapsed_ms=elapsed_ms)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
