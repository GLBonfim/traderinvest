"""Historical paper-trading replay of the six baselines (no live trading, no broker).

uv run python -m app.paper.cli replay --symbol SPY [--scenario control_no_overlay]
                                   [--out data/paper]
"""

import argparse
import json
import sys
import time
from pathlib import Path

from app.backtest.data import load_backtest_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.safety import assert_safe_trading_mode
from app.database.session import get_session_factory
from app.paper.config import PaperConfig
from app.paper.engine import replay_baselines
from app.paper.ledger import manifest, write_ledger
from app.risk.config import SCENARIOS

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.paper.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("replay", help="Historical replay through risk -> PaperBroker")
    rep.add_argument("--symbol", required=True)
    rep.add_argument(
        "--scenario", default="control_no_overlay", choices=[s.name for s in SCENARIOS]
    )
    rep.add_argument("--out", type=Path, default=Path("data/paper"))
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    assert_safe_trading_mode(settings)  # real-money execution stays blocked
    config = PaperConfig(risk=next(s for s in SCENARIOS if s.name == args.scenario))
    with get_session_factory()() as session:
        _, bars = load_backtest_bars(session, args.symbol.upper())
    started = time.perf_counter()
    runs = replay_baselines(bars, config)
    elapsed = round(time.perf_counter() - started, 1)
    out = write_ledger(runs, config, args.out)
    meta = manifest(runs, config)
    log.info("paper.replay", scenario=args.scenario, elapsed_s=elapsed, output=str(out))
    print(
        json.dumps(
            {
                **{
                    k: meta[k]
                    for k in (
                        "mode",
                        "note",
                        "git_commit",
                        "config_fingerprint",
                        "risk_scenario",
                        "strategies",
                    )
                },
                "output": str(out),
                "elapsed_s": elapsed,
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
