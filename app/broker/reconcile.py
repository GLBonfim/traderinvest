"""Reconciliation: local paper simulation vs the external SANDBOX account (read-only).

For every linked paper account the expected sandbox quantity is what the executor targets:
0 if the local simulation is flat; otherwise target_quantity(approved exposure of the decision
behind the last local fill x allocated capital / that decision's close) under the configured
order policy — the same sizing the order was built with, so price moves after the decision do
not create false mismatches. Actual = sandbox position. A difference of at least one share
(whole-share policy) or any difference above 1e-9 (fractional policy) is a mismatch. Recorded
sandbox orders whose fill quantity differs from the requested quantity (paper partial fills) are
listed too. Nothing is corrected automatically: reconciliation never submits an order.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.alerts.models import AlertEvent, make_alert
from app.alerts.monitor import paper_account_dirs, read_paper_account
from app.broker.config import OPG_WHOLE_SHARES, BrokerConfig
from app.broker.executor import SANDBOX_TAG, target_quantity
from app.broker.gateway import BrokerGateway
from app.broker.store import BrokerStore


def expected_quantity(events: list[dict[str, Any]], config: BrokerConfig) -> tuple[float, str]:
    """(expected sandbox qty, basis) from the local ledger: the last fill's originating decision."""
    fills = [e["record"] for e in events if e["event_type"] == "fill"]
    if not fills or float(fills[-1]["position_after"]) <= 0:
        return 0.0, "local simulation flat"
    orders = {e["record"]["order_id"]: e["record"] for e in events if e["event_type"] == "order"}
    decisions = {
        e["record"]["decision_id"]: e["record"] for e in events if e["event_type"] == "decision"
    }
    snaps = {e["record"]["ts"]: e["record"] for e in events if e["event_type"] == "snapshot"}
    d = decisions[orders[fills[-1]["order_id"]]["decision_id"]]
    price = float(snaps[d["bar_ts"]]["mark_price"])
    qty = target_quantity(
        float(d["risk_approved_target"]), config.allocated_capital, price, config.order_policy
    )
    return qty, f"decision {d['decision_id']} (exposure {d['risk_approved_target']}, close {price})"


@dataclass
class Reconciliation:
    at: str
    accounts: list[dict[str, Any]] = field(default_factory=list)
    partial_fills: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[AlertEvent] = field(default_factory=list)

    @property
    def mismatches(self) -> int:
        return sum(1 for a in self.accounts if a["status"] == "mismatch")


def reconcile(
    config: BrokerConfig,
    store: BrokerStore,
    gateway: BrokerGateway,
    paper_root: Path,
    now: datetime,
) -> Reconciliation:
    rec = Reconciliation(now.isoformat())
    dirs = {p.name: p for p in paper_account_dirs(paper_root)}
    tol = 1.0 if config.order_policy == OPG_WHOLE_SHARES else 1e-9
    for acct in store.linked_accounts:
        if acct not in dirs:
            rec.accounts.append({"account_id": acct, "status": "missing_local_account"})
            continue
        paper = read_paper_account(dirs[acct])
        snaps = [e for e in paper.events if e["event_type"] == "snapshot"]
        if paper.error_kind or not snaps:
            rec.accounts.append(
                {"account_id": acct, "status": paper.error_kind or "no_local_history"}
            )
            continue
        s = snaps[-1]["record"]
        expected, basis = expected_quantity(paper.events, config)
        pos = gateway.position(paper.instrument)
        actual = 0.0 if pos is None else pos.qty
        status = "ok" if abs(actual - expected) < tol else "mismatch"
        row = {
            "account_id": acct,
            "symbol": paper.instrument,
            "session": snaps[-1]["session"],
            "local_exposure": s["gross_exposure"],
            "sizing_basis": basis,
            "expected_sandbox_qty": expected,
            "sandbox_qty": actual,
            "difference": actual - expected,
            "status": status,
        }
        rec.accounts.append(row)
        if status == "mismatch":
            rec.alerts.append(
                make_alert(
                    "BROKER_RECONCILIATION_MISMATCH",
                    key_parts=(acct, snaps[-1]["session"]),
                    occurred_at=now.isoformat(),
                    account_id=acct,
                    instrument=paper.instrument,
                    title=f"{SANDBOX_TAG}: sandbox position differs from the paper simulation",
                    message=f"{SANDBOX_TAG}. Expected {expected:g} {paper.instrument} from the "
                    f"local simulation, sandbox holds {actual:g}. Nothing is corrected "
                    "automatically.",
                    payload=row,
                )
            )
    for cid, o in store.orders().items():
        req = (o.get("order") or {}).get("qty")
        if (
            req is not None
            and o.get("status") == "filled"
            and abs(float(o.get("filled_qty") or 0) - float(req)) > 1e-9
        ):
            rec.partial_fills.append(
                {"client_order_id": cid, "requested": req, "filled": o.get("filled_qty")}
            )
    return rec
