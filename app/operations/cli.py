"""Daily operations CLI — local, paper-only (PAPER SIMULATION / LOCAL OPERATIONS).

uv run python -m app.operations.cli run [--dry-run]     process pending completed sessions
uv run python -m app.operations.cli status              checkpoint, lock, scheduler, metrics
uv run python -m app.operations.cli history [--limit N] recent runs (stages, statuses)
uv run python -m app.operations.cli verify              operations-store integrity
uv run python -m app.operations.cli next                next eligible session / pending work
uv run python -m app.operations.cli scheduler           run the local scheduler (Ctrl+C stops)
uv run python -m app.operations.cli unlock --force      remove a lock after verifying its owner
"""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.safety import assert_safe_trading_mode
from app.data.calendar import TradingCalendar
from app.database.session import get_engine
from app.operations import sessions as cal_
from app.operations.config import ALERTS_ROOT, OPS_ROOT, PAPER_ROOT, OperationsConfig
from app.operations.lock import PipelineLock
from app.operations.pipeline import Pipeline
from app.operations.scheduler import Scheduler, scheduler_status
from app.operations.services import SystemServices
from app.operations.store import OpsStore, OpsStoreError


def build_pipeline(config: OperationsConfig, *, dry_run: bool = False) -> Pipeline:
    services = SystemServices(
        config,
        engine=get_engine(),
        settings=get_settings(),
        paper_root=PAPER_ROOT,
        alerts_root=ALERTS_ROOT,
        ops_root=OPS_ROOT,
    )
    return Pipeline(config, services, OpsStore(OPS_ROOT, no_write=dry_run))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.operations.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Process pending completed sessions (PAPER SIMULATION)")
    run.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    sub.add_parser("status")
    hist = sub.add_parser("history")
    hist.add_argument("--limit", type=int, default=10)
    sub.add_parser("verify")
    sub.add_parser("next")
    sch = sub.add_parser("scheduler", help="Run the local scheduler loop")
    sch.add_argument("--max-ticks", type=int, default=None)
    unl = sub.add_parser("unlock", help="Remove the pipeline lock (operator action)")
    unl.add_argument("--force", action="store_true", required=True)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    assert_safe_trading_mode(settings)
    cfg = OperationsConfig()
    now = datetime.now(UTC)
    out: object
    if args.command == "run":
        res = build_pipeline(cfg, dry_run=args.dry_run).run(
            mode="dry_run" if args.dry_run else "manual"
        )
        out = asdict(res)
    elif args.command == "status":
        st = OpsStore.read_only(OPS_ROOT)
        out = {
            "checkpoint": None if st is None else st.state["last_completed_session"],
            "last_run": None if st is None else st.state["last_run"],
            "lock": PipelineLock(OPS_ROOT).owner(),
            "scheduler": scheduler_status(st, now, cfg.scheduler_poll_seconds),
            "metrics": None if st is None else st.metrics(),
        }
    elif args.command == "history":
        st = OpsStore.read_only(OPS_ROOT)
        out = [] if st is None else st.history(args.limit)
    elif args.command == "verify":
        try:
            st = OpsStore(OPS_ROOT, create=False)
            out = {"status": "ok", "events": len(st.events()), "runs": len(st.history(10**9))}
        except OpsStoreError as exc:
            out = {"status": "failed", "error": str(exc)}
    elif args.command == "next":
        cal = TradingCalendar(cfg.exchange)
        plan = build_pipeline(cfg, dry_run=True).plan(now)
        out = {
            "now": now.isoformat(),
            "latest_eligible_session": str(plan.latest_eligible),
            "next_session_eligible_at": cal_.next_eligibility(
                cal, now, cfg.grace_minutes
            ).isoformat(),
            "checkpoint": str(plan.checkpoint),
            "pending_sessions": [s.isoformat() for s in plan.pending],
        }
    elif args.command == "scheduler":
        sched = Scheduler(cfg, lambda: build_pipeline(cfg), OpsStore(OPS_ROOT))
        out = [asdict(t) for t in sched.run_forever(max_ticks=args.max_ticks)]
    else:
        out = {"removed_lock": PipelineLock(OPS_ROOT).force_release()}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
