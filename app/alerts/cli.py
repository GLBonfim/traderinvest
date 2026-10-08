"""Alerts & monitoring CLI (local; alerts never trigger any trading action).

uv run python -m app.alerts.cli run [--since YYYY-MM-DD] [--no-deliver]
uv run python -m app.alerts.cli list [--severity WARNING] [--source RiskManager] [--limit 20]
uv run python -m app.alerts.cli status
uv run python -m app.alerts.cli verify
uv run python -m app.alerts.cli test-channel [--channel console|webhook]
"""

import argparse
import json
import sys
from datetime import UTC, date, datetime

from app.alerts.channels import as_dict, configured_channels, deliver_pending
from app.alerts.engine import DEFAULT_ALERTS_ROOT, DEFAULT_PAPER_ROOT, run_alerts, verify_store
from app.alerts.models import SEVERITY_RANK, make_alert
from app.alerts.sources import dashboard_results_missing, load_market
from app.alerts.store import AlertStore
from app.core.config import get_settings
from app.core.logging import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.alerts.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Evaluate alert rules, store new alerts, deliver")
    run.add_argument(
        "--since",
        type=date.fromisoformat,
        default=None,
        help="reporting start for a NEW store (default: latest stored session)",
    )
    run.add_argument("--no-deliver", action="store_true")
    ls = sub.add_parser("list", help="List stored alerts (newest first)")
    ls.add_argument("--severity", default=None, choices=sorted(SEVERITY_RANK))
    ls.add_argument("--source", default=None)
    ls.add_argument("--limit", type=int, default=20)
    sub.add_parser("status", help="Store metrics and delivery status")
    sub.add_parser("verify", help="Verify the alert store hash chains")
    tc = sub.add_parser("test-channel", help="Send an explicit TEST notification (not market)")
    tc.add_argument("--channel", default="console", choices=["console", "webhook"])
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    now = datetime.now(UTC)

    if args.command == "run":
        res = run_alerts(
            now=now,
            settings=settings,
            alerts_root=DEFAULT_ALERTS_ROOT,
            paper_root=DEFAULT_PAPER_ROOT,
            load_market=load_market,
            dashboard_results_missing=dashboard_results_missing,
            deliver=not args.no_deliver,
            since=args.since,
        )
        out = {
            "evaluated_at": res.evaluated_at,
            "reporting_since": res.since,
            "candidates": res.candidates,
            "new_alerts": len(res.new_alerts),
            "new_by_type": {
                t: sum(1 for a in res.new_alerts if a.event_type == t)
                for t in sorted({a.event_type for a in res.new_alerts})
            },
            "delivery": None if res.delivery is None else as_dict(res.delivery),
            "health": {k: v["status"] for k, v in res.health.items() if isinstance(v, dict)},
            "overall": res.health["overall"],
        }
    elif args.command == "list":
        alerts = AlertStore(DEFAULT_ALERTS_ROOT, create=False).alerts()[::-1]
        if args.severity:
            alerts = [a for a in alerts if a["severity"] == args.severity]
        if args.source:
            alerts = [a for a in alerts if a["source"] == args.source]
        out = {
            "alerts": [
                {
                    k: a[k]
                    for k in (
                        "alert_id",
                        "occurred_at",
                        "severity",
                        "source",
                        "event_type",
                        "title",
                    )
                }
                for a in alerts[: args.limit]
            ]
        }
    elif args.command == "status":
        st = AlertStore(DEFAULT_ALERTS_ROOT, create=False)
        out = {
            "since": st.state["since"],
            "metrics": st.metrics(),
            "active_conditions": st.state["conditions"],
            "channels": [repr(c) for c in configured_channels()],
        }
    elif args.command == "verify":
        out = verify_store(DEFAULT_ALERTS_ROOT)
    else:
        st = AlertStore(DEFAULT_ALERTS_ROOT)
        channels = [c for c in configured_channels() if c.name == args.channel]
        if not channels:
            parser.error(f"channel {args.channel!r} is not configured")
        test = make_alert(
            "TEST_NOTIFICATION",
            key_parts=(args.channel, now.isoformat()),
            occurred_at=now.isoformat(),
            title="Test notification (not a market event)",
            message="Explicit channel test requested from the CLI. Not a market or trading event.",
            payload={"channel": args.channel},
        )
        st.add([test], recorded_at=now.isoformat())
        out = {
            "test_alert_id": test.alert_id,
            "delivery": as_dict(deliver_pending(st, channels, now, only={test.alert_id})),
        }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
