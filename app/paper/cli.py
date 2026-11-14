"""Paper trading CLI — a local simulation; it does not transmit orders to any brokerage or market.

uv run python -m app.paper.cli replay --symbol SPY [--scenario control_no_overlay]
                                      [--start YYYY-MM-DD] [--out data/paper]
uv run python -m app.paper.cli run    --symbol SPY [--strategy sma_trend ...]
                                      [--scenario control_no_overlay] [--start YYYY-MM-DD]
                                      [--until YYYY-MM-DD] [--root data/paper/accounts]
uv run python -m app.paper.cli status --symbol SPY [--strategy ...] [--scenario ...]

`run` processes every completed bar in the local database that the account has not processed
yet (restart-safe, idempotent); bars come from the database only — this module never fetches
data or connects to anything.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from app.backtest.data import load_backtest_bars
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.safety import assert_safe_trading_mode
from app.database.session import get_session_factory
from app.paper.config import PaperConfig
from app.paper.engine import baseline_inputs, replay_baselines
from app.paper.ledger import manifest, write_ledger
from app.paper.store import DEFAULT_ROOT, PaperStore, conflicting_accounts
from app.paper.trader import account_id_for
from app.risk.config import SCENARIOS

log = get_logger(__name__)
TIMEFRAME = "1d"  # the only timeframe of incremental paper accounts (PaperStore default)
EXIT_REFUSED = 2  # an account was refused (identity conflict); nothing was created


def _ny_date(ts: pd.Timestamp) -> date:
    return pd.Timestamp(ts).tz_convert("America/New_York").date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.paper.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    scen = [s.name for s in SCENARIOS]
    rep = sub.add_parser("replay", help="Historical replay through risk -> PaperBroker")
    run = sub.add_parser("run", help="Incremental, restart-safe processing of completed bars")
    stat = sub.add_parser("status", help="Show the stored state of paper accounts")
    for p in (rep, run, stat):
        p.add_argument("--symbol", required=True)
        p.add_argument("--scenario", default="control_no_overlay", choices=scen)
    rep.add_argument("--start", type=date.fromisoformat, default=None)
    rep.add_argument("--out", type=Path, default=Path("data/paper"))
    for p in (run, stat):
        p.add_argument("--strategy", action="append", default=None, help="repeatable; default all")
        p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    run.add_argument(
        "--start",
        type=date.fromisoformat,
        default=None,
        help="first session of a NEW account (ignored once it exists)",
    )
    run.add_argument(
        "--until",
        type=date.fromisoformat,
        default=None,
        help="process completed bars up to this session date only",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    assert_safe_trading_mode(settings)  # real-money execution stays blocked
    config = PaperConfig(risk=next(s for s in SCENARIOS if s.name == args.scenario))
    symbol = args.symbol.upper()
    with get_session_factory()() as session:
        _, bars = load_backtest_bars(session, symbol)
    if getattr(args, "until", None):
        bars = bars[[_ny_date(t) <= args.until for t in bars.index]]

    started = time.perf_counter()
    refused = False
    if args.command == "replay":
        runs = replay_baselines(bars, config, instrument=symbol, start=args.start)
        out = write_ledger(runs, config, args.out)
        meta = manifest(runs, config)
        keys = ("mode", "note", "git_commit", "config_fingerprint", "risk_scenario", "strategies")
        result = {**{k: meta[k] for k in keys}, "output": str(out)}
    else:
        inputs = baseline_inputs(bars)
        wanted = args.strategy or list(inputs)
        unknown = set(wanted) - set(inputs)
        if unknown:
            parser.error(f"unknown strategies: {sorted(unknown)}")
        result = {"mode": "paper_simulation:incremental", "accounts": {}}
        for sid in wanted:
            version, bar_inputs = inputs[sid]
            expected = account_id_for(config, sid, version, symbol, TIMEFRAME)
            if not (args.root / expected / "state.json").exists():  # no account yet
                others = conflicting_accounts(
                    args.root,
                    account_id=expected,
                    strategy_id=sid,
                    scenario=config.risk.name,
                    instrument=symbol,
                    timeframe=TIMEFRAME,
                )
                if args.command == "status":  # read-only: never creates an account
                    result["accounts"][sid] = {
                        "status": "no_account",
                        "account_id": expected,
                        "incompatible_accounts": others,
                    }
                    continue
                if others:  # never a silent parallel account next to an existing one
                    refused = True
                    result["accounts"][sid] = {
                        "status": "refused",
                        "account_id": expected,
                        "incompatible_accounts": others,
                        "error": (
                            f"account(s) {others} exist for {sid} / {config.risk.name} / "
                            f"{symbol} / {TIMEFRAME} under another identity (an earlier "
                            "configuration, risk-mechanism semantic version — ADR-0034 — or "
                            f"strategy version); not creating {expected} next to them. They "
                            "were not opened or changed."
                        ),
                    }
                    log.error("paper.account_identity_conflict", strategy=sid,
                              account_id=expected, existing=others)  # fmt: skip
                    continue
            store = PaperStore(
                config, strategy_id=sid, strategy_version=version, instrument=symbol, root=args.root
            )
            if args.command == "run":
                counts = store.process_many(bar_inputs, start=args.start)
                result["accounts"][sid] = {**counts, **store.status()}
            else:
                result["accounts"][sid] = store.status()
    result["elapsed_s"] = round(time.perf_counter() - started, 2)
    log.info(f"paper.{args.command}", scenario=args.scenario, elapsed_s=result["elapsed_s"])
    print(json.dumps(result, indent=2, default=str))
    return EXIT_REFUSED if refused else 0


if __name__ == "__main__":
    sys.exit(main())
