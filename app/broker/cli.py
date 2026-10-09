"""Broker SANDBOX CLI — Alpaca PAPER account only, no real money.

uv run python -m app.broker.cli status                 configuration, gates, linked accounts
uv run python -m app.broker.cli link <account_id>      mirror a local paper account (unlink too)
uv run python -m app.broker.cli sync                   DRY RUN: what would be sent (default)
uv run python -m app.broker.cli sync --submit          send pending risk-approved instructions
                                                       (only if armed, kill switch released,
                                                       TRADING_MODE=paper, sandbox keys set)
uv run python -m app.broker.cli reconcile              local simulation vs sandbox (read-only)
uv run python -m app.broker.cli arm --confirm-sandbox-only | disarm
uv run python -m app.broker.cli kill [--reason ...] | release-kill --confirm
uv run python -m app.broker.cli verify                 order-log integrity

There is deliberately no command to place an arbitrary order and no option to choose an
endpoint: orders come only from RiskManager-approved paper decisions, and the endpoint is the
fixed sandbox URL.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from app.alerts.channels import configured_channels, deliver_pending
from app.alerts.models import AlertEvent, make_alert
from app.alerts.store import AlertStore
from app.broker.alpaca import AlpacaPaperGateway
from app.broker.config import BROKER_ROOT, KEY_ID_ENV, SANDBOX_BASE_URL, SECRET_ENV, BrokerConfig
from app.broker.executor import SANDBOX_TAG, SandboxExecutor
from app.broker.gateway import BrokerSafetyError
from app.broker.reconcile import reconcile
from app.broker.store import BrokerStore, BrokerStoreError
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.safety import assert_safe_trading_mode
from app.operations.config import ALERTS_ROOT, PAPER_ROOT


def gateway_from_env(cfg: BrokerConfig) -> AlpacaPaperGateway | None:
    try:
        return AlpacaPaperGateway.from_env(os.environ, timeout=cfg.request_timeout_seconds)
    except BrokerSafetyError:
        return None


def emit(alerts: list[AlertEvent], now: datetime) -> None:
    if alerts:
        st = AlertStore(ALERTS_ROOT)
        st.add(alerts, recorded_at=now.isoformat())
        deliver_pending(st, configured_channels(), now)


def status(cfg: BrokerConfig, store: BrokerStore) -> dict[str, object]:
    s = get_settings()
    return {
        "environment": "SANDBOX (Alpaca paper account; no real money)",
        "endpoint": SANDBOX_BASE_URL,
        "credentials_present": bool(os.environ.get(KEY_ID_ENV) and os.environ.get(SECRET_ENV)),
        "trading_mode": s.trading_mode.value,
        "armed": store.armed,
        "kill_switch": store.state["kill_switch"],
        "linked_accounts": store.linked_accounts,
        "order_policy": cfg.order_policy,
        "limits": {
            "allocated_capital": cfg.allocated_capital,
            "max_order_notional": cfg.max_order_notional,
            "max_orders_per_day": cfg.max_orders_per_day,
            "allowed_symbols": list(cfg.allowed_symbols),
        },
        "orders_recorded": len(store.orders()),
        "counters": store.state["counters"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.broker.cli")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for name in ("link", "unlink"):
        sub.add_parser(name).add_argument("account_id")
    sy = sub.add_parser("sync")
    sy.add_argument("--submit", action="store_true", help="actually send (default: dry run)")
    sub.add_parser("reconcile")
    arm = sub.add_parser("arm")
    arm.add_argument("--confirm-sandbox-only", action="store_true", required=True)
    sub.add_parser("disarm")
    kill = sub.add_parser("kill")
    kill.add_argument("--reason", default="operator")
    rel = sub.add_parser("release-kill")
    rel.add_argument("--confirm", action="store_true", required=True)
    sub.add_parser("verify")
    args = p.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    assert_safe_trading_mode(settings)
    cfg = BrokerConfig()
    now = datetime.now(UTC)
    out: object
    if args.command == "verify":
        try:
            st = BrokerStore(BROKER_ROOT, no_write=True)
            out = {"status": "ok", "events": len(st.events())}
        except BrokerStoreError as exc:
            out = {"status": "failed", "error": str(exc)}
        print(json.dumps(out, indent=2))
        return 0
    store = BrokerStore(
        BROKER_ROOT,
        no_write=args.command in ("status", "reconcile")
        or (args.command == "sync" and not args.submit),
    )
    if args.command == "status":
        out = status(cfg, store)
    elif args.command == "link":
        store.link(args.account_id)
        out = {"linked_accounts": store.linked_accounts}
    elif args.command == "unlink":
        store.unlink(args.account_id)
        out = {"linked_accounts": store.linked_accounts}
    elif args.command == "arm":
        store.set_armed(True)
        out = {"armed": True, "note": f"{SANDBOX_TAG}. Orders are sent only by `sync --submit`."}
    elif args.command == "disarm":
        store.set_armed(False)
        out = {"armed": False}
    elif args.command == "kill":
        store.set_kill_switch(True, args.reason)
        emit(
            [
                make_alert(
                    "BROKER_KILL_SWITCH_ENGAGED",
                    key_parts=("kill", now.isoformat()),
                    occurred_at=now.isoformat(),
                    title=f"{SANDBOX_TAG}: kill switch engaged",
                    message=f"{SANDBOX_TAG}. No sandbox order will be sent until the kill "
                    "switch is released explicitly.",
                    payload={"reason": args.reason},
                )
            ],
            now,
        )
        out = {"kill_switch": store.state["kill_switch"]}
    elif args.command == "release-kill":
        store.set_kill_switch(False, "released by operator")
        out = {"kill_switch": store.state["kill_switch"]}
    elif args.command == "sync":
        ex = SandboxExecutor(cfg, store, settings, PAPER_ROOT, gateway_from_env(cfg))
        rep = ex.sync(dry_run=not args.submit)
        if not rep.dry_run:
            emit(rep.alerts, now)
        out = {
            "dry_run": rep.dry_run,
            "items": [asdict(i) for i in rep.items],
            "reconciled": rep.reconciled,
        }
    else:  # reconcile
        gw = gateway_from_env(cfg)
        if gw is None:
            out = {"status": "unavailable", "reason": "sandbox credentials not set"}
        else:
            rec = reconcile(cfg, store, gw, PAPER_ROOT, now)
            emit(rec.alerts, now)
            out = {
                "accounts": rec.accounts,
                "partial_fills": rec.partial_fills,
                "mismatches": rec.mismatches,
            }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
