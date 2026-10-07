"""Command line entry point for market-data ingestion.

    uv run python -m app.data.cli ingest --symbol SPY --start 1993-01-01
"""

import argparse
import json
import sys
from datetime import UTC, date, datetime

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.data.ingestion import ingest_daily_bars
from app.data.instruments import get_spec
from app.data.providers.yfinance_provider import YFinanceProvider
from app.database.session import get_session_factory

PROVIDERS = {YFinanceProvider.name: YFinanceProvider}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.data.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", help="Ingest daily OHLCV bars")
    ing.add_argument("--symbol", required=True)
    ing.add_argument("--provider", default="yfinance", choices=sorted(PROVIDERS))
    ing.add_argument("--start", required=True, type=date.fromisoformat)
    ing.add_argument("--end", type=date.fromisoformat, default=None, help="inclusive; default today (UTC)")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    end: date = args.end or datetime.now(UTC).date()
    with get_session_factory()() as session:
        result = ingest_daily_bars(
            session, PROVIDERS[args.provider](), get_spec(args.symbol), args.start, end
        )
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
