"""Broker SANDBOX status for the dashboard (read-only, never cached). The only control the
dashboard offers is engaging the kill switch; arming, releasing and submitting are CLI-only."""

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.alerts.channels import configured_channels, deliver_pending
from app.alerts.models import make_alert
from app.alerts.store import AlertStore
from app.broker.cli import gateway_from_env, status
from app.broker.config import BrokerConfig
from app.broker.executor import SANDBOX_TAG, SandboxExecutor
from app.broker.store import BrokerStore
from app.core.config import get_settings


def overview(broker_root: Path) -> dict[str, Any]:
    return status(BrokerConfig(), BrokerStore(broker_root, no_write=True))


def orders_table(broker_root: Path) -> pd.DataFrame:
    rows = list(BrokerStore(broker_root, no_write=True).orders().values())
    cols = [
        "client_order_id",
        "status",
        "broker_order_id",
        "filled_qty",
        "filled_avg_price",
        "reason",
        "error",
        "at",
    ]
    return pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)


def preview(broker_root: Path, paper_root: Path) -> list[dict[str, Any]]:
    """Dry run: what `sync --submit` would do now. Sends nothing and writes nothing."""
    cfg = BrokerConfig()
    ex = SandboxExecutor(
        cfg,
        BrokerStore(broker_root, no_write=True),
        get_settings(),
        paper_root,
        gateway_from_env(cfg),
    )
    return [asdict(i) for i in ex.sync(dry_run=True).items]


def engage_kill_switch(broker_root: Path, alerts_root: Path, reason: str) -> None:
    store = BrokerStore(broker_root)
    store.set_kill_switch(True, reason)
    now = datetime.now(UTC)
    alert = make_alert(
        "BROKER_KILL_SWITCH_ENGAGED",
        key_parts=("kill", now.isoformat()),
        occurred_at=now.isoformat(),
        title=f"{SANDBOX_TAG}: kill switch engaged",
        message=f"{SANDBOX_TAG}. No sandbox order will be sent until it is released from the CLI.",
        payload={"reason": reason, "source": "dashboard"},
    )
    st = AlertStore(alerts_root)
    st.add([alert], recorded_at=now.isoformat())
    deliver_pending(st, configured_channels(), now)
