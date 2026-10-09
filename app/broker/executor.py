"""Sandbox executor: mirrors RiskManager-approved paper instructions into the Alpaca PAPER
account. SANDBOX / PAPER ONLY — no real money, no production endpoint.

Source of every order: the latest committed, hash-verified decision of a LINKED local paper
account (Phase 12 ledger) whose risk-approved instruction is pending (entry, exit, rebalance)
and whose scheduled session open is still in the future. There is no API to submit an
arbitrary/manual order: `sync()` only ever derives orders from those decisions.

Sizing (deterministic, point-in-time): target qty = approved exposure x allocated capital /
decision close; whole shares (floor) under the default `opg_whole_shares` policy (opening
auction, the closest match to the project's next-session-open convention); delta = target qty -
current sandbox position; an exit sells exactly the sandbox position. Never short.

Gates, in order (a failing gate blocks and is recorded; nothing is raised): kill switch, armed,
trading mode == paper, sandbox credentials present, symbol allowed, not already submitted,
no short, notional cap, daily order cap. Idempotency: deterministic client_order_id
(`qp-<account>-<decision>`), write-ahead `submit_attempted`, lookup by client_order_id before
every POST and after any ambiguous failure.
"""

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.alerts.models import AlertEvent, make_alert
from app.alerts.monitor import paper_account_dirs, read_paper_account
from app.broker.config import OPG_WHOLE_SHARES, BrokerConfig
from app.broker.gateway import BrokerGateway, BrokerRequestError, OrderRequest
from app.broker.store import BrokerStore
from app.core.config import Settings, TradingMode

SANDBOX_TAG = "BROKER SANDBOX (paper account, no real money)"
EPS = 1e-9


@dataclass(frozen=True)
class Intent:
    client_order_id: str
    account_id: str
    decision_id: str
    symbol: str
    pending_kind: str  # entry | exit | rebalance
    approved_exposure: float
    reference_price: float  # close of the decision bar
    observed_at: str
    scheduled_for: str
    target_qty: float


@dataclass
class SyncItem:
    account_id: str
    outcome: (
        str  # no_pending_instruction | no_order_required | would_submit | submitted | blocked | ...
    )
    detail: str = ""
    intent: dict[str, Any] | None = None
    order: dict[str, Any] | None = None


@dataclass
class SyncReport:
    dry_run: bool
    at: str
    items: list[SyncItem] = field(default_factory=list)
    reconciled: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[AlertEvent] = field(default_factory=list)


def target_quantity(exposure: float, capital: float, price: float, policy: str) -> float:
    raw = max(0.0, exposure) * capital / price
    if policy == OPG_WHOLE_SHARES:
        return float(math.floor(raw + EPS))
    return math.floor(raw * 1e9) / 1e9


def derive_intent(account_dir: Path, config: BrokerConfig, now: datetime) -> Intent | None:
    """The pending, risk-approved instruction of a paper account, if it requires an order."""
    acc = read_paper_account(account_dir)
    if acc.error_kind:
        raise RuntimeError(f"paper account {acc.account_id} unreadable: {acc.error_kind}")
    decisions = [e["record"] for e in acc.events if e["event_type"] == "decision"]
    snaps = [e["record"] for e in acc.events if e["event_type"] == "snapshot"]
    if not decisions or not snaps:
        return None
    d, s = decisions[-1], snaps[-1]
    if d["pending_kind"] not in ("entry", "exit", "rebalance"):
        return None
    if pd.Timestamp(d["scheduled_execution_at"]) <= pd.Timestamp(now):
        return None  # its session already opened: never send a stale instruction
    price = float(s["mark_price"])
    exposure = float(d["risk_approved_target"])
    return Intent(
        client_order_id=f"qp-{acc.account_id}-{d['decision_id'].split('-')[-1]}",
        account_id=acc.account_id,
        decision_id=d["decision_id"],
        symbol=acc.instrument,
        pending_kind=d["pending_kind"],
        approved_exposure=exposure,
        reference_price=price,
        observed_at=d["observed_at"],
        scheduled_for=d["scheduled_execution_at"],
        target_qty=target_quantity(exposure, config.allocated_capital, price, config.order_policy),
    )


class SandboxExecutor:
    def __init__(
        self,
        config: BrokerConfig,
        store: BrokerStore,
        settings: Settings,
        paper_root: Path,
        gateway: BrokerGateway | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = config
        self.store = store
        self.settings = settings
        self.paper_root = paper_root
        self.gateway = gateway
        self.clock = clock or (lambda: datetime.now(UTC))

    def _alert(
        self,
        report: SyncReport,
        etype: str,
        key: tuple[str, ...],
        title: str,
        message: str,
        payload: dict[str, Any],
        account: str | None,
    ) -> None:
        report.alerts.append(
            make_alert(
                etype,
                key_parts=key,
                occurred_at=report.at,
                title=f"{SANDBOX_TAG}: {title}",
                message=f"{SANDBOX_TAG}. {message}",
                payload=payload,
                account_id=account,
                instrument=payload.get("symbol"),
            )
        )

    def _gate(self, intent: Intent) -> str:
        if self.store.kill_switch:
            return "kill_switch_engaged"
        if not self.store.armed:
            return "not_armed"
        if self.settings.trading_mode != TradingMode.PAPER:
            return f"trading_mode_{self.settings.trading_mode.value}_is_not_paper"
        if self.gateway is None:
            return "sandbox_credentials_missing"
        if intent.symbol not in self.cfg.allowed_symbols:
            return "symbol_not_allowed"
        return ""

    def sync(self, *, dry_run: bool = True) -> SyncReport:
        now = self.clock()
        report = SyncReport(dry_run, now.isoformat())
        if not dry_run:
            self.store.bump("syncs")
            self._refresh_open_orders(report)
        dirs = {p.name: p for p in paper_account_dirs(self.paper_root)}
        for acct in self.store.linked_accounts:
            if acct not in dirs:
                report.items.append(SyncItem(acct, "blocked", "linked account not found"))
                continue
            try:
                intent = derive_intent(dirs[acct], self.cfg, now)
            except RuntimeError as exc:
                report.items.append(SyncItem(acct, "blocked", str(exc)))
                continue
            if intent is None:
                report.items.append(SyncItem(acct, "no_pending_instruction"))
                continue
            report.items.append(self._one(intent, report, dry_run, now))
        return report

    def _one(self, intent: Intent, report: SyncReport, dry_run: bool, now: datetime) -> SyncItem:
        item = SyncItem(intent.account_id, "", intent=asdict(intent))
        known = self.store.orders().get(intent.client_order_id)
        # a write-ahead attempt confirmed absent at the broker may be retried (same id)
        if known and known["status"] not in ("intent", "blocked", "not_found_at_broker"):
            item.outcome, item.detail = "already_submitted", f"status {known['status']}"
            return item
        reason = self._gate(intent)
        if reason:
            item.outcome, item.detail = "blocked", reason
            if not dry_run and reason not in ("not_armed",):
                self.store.record(
                    "blocked", intent.client_order_id, reason=reason, intent=asdict(intent)
                )
                self.store.bump("blocked")
                self._alert(
                    report,
                    "BROKER_ORDER_BLOCKED",
                    (intent.client_order_id, reason),
                    f"order blocked ({reason})",
                    f"A risk-approved instruction was not sent: {reason}.",
                    {
                        "symbol": intent.symbol,
                        "reason": reason,
                        "client_order_id": intent.client_order_id,
                    },
                    intent.account_id,
                )
            return item
        if self.gateway is None:  # guarded by _gate; kept explicit
            raise RuntimeError("no sandbox gateway")
        pos = self.gateway.position(intent.symbol)
        held = 0.0 if pos is None else pos.qty
        if held < -EPS:
            item.outcome, item.detail = "blocked", "sandbox position is short; refusing to trade"
            return item
        target = 0.0 if intent.pending_kind == "exit" else intent.target_qty
        delta = target - held
        if abs(delta) < EPS or (self.cfg.order_policy == OPG_WHOLE_SHARES and abs(delta) < 1):
            item.outcome, item.detail = (
                "no_order_required",
                f"sandbox holds {held}, target {target}",
            )
            return item
        side = "buy" if delta > 0 else "sell"
        qty = abs(delta) if side == "buy" else min(abs(delta), held)
        notional = qty * intent.reference_price
        if notional > self.cfg.max_order_notional:
            reason = f"notional {notional:.2f} exceeds cap {self.cfg.max_order_notional:.2f}"
            item.outcome, item.detail = "blocked", reason
            if not dry_run:
                self.store.record("blocked", intent.client_order_id, reason=reason)
            return item
        if self.store.submitted_today(now.date().isoformat()) >= self.cfg.max_orders_per_day:
            item.outcome, item.detail = "blocked", "daily order cap reached"
            return item
        req = OrderRequest(
            intent.client_order_id,
            intent.symbol,
            side,
            qty,
            "opg" if self.cfg.order_policy == OPG_WHOLE_SHARES else "day",
        )
        item.order = asdict(req)
        if dry_run:
            item.outcome = "would_submit"
            return item
        self.store.record("intent", req.client_order_id, intent=asdict(intent), order=asdict(req))
        self.store.record("submit_attempted", req.client_order_id)  # write-ahead
        self._submit(req, intent, item, report)
        return item

    def _submit(
        self, req: OrderRequest, intent: Intent, item: SyncItem, report: SyncReport
    ) -> None:
        if self.gateway is None:  # guarded by _gate; kept explicit
            raise RuntimeError("no sandbox gateway")
        try:
            existing = self.gateway.order_by_client_id(req.client_order_id)
            order = existing or self.gateway.submit(req)
        except BrokerRequestError as exc:
            if exc.ambiguous:
                self.store.record("unknown", req.client_order_id, error=str(exc))
                item.outcome, item.detail = "unknown", "outcome unknown; resolved on next sync"
            else:
                self.store.record("failed", req.client_order_id, error=str(exc), status=exc.status)
                self.store.bump("failed")
                item.outcome, item.detail = "failed", str(exc)
                self._alert(
                    report,
                    "BROKER_ORDER_FAILED",
                    (req.client_order_id,),
                    "sandbox order failed",
                    f"The sandbox rejected the order: {exc}.",
                    {"symbol": req.symbol, "client_order_id": req.client_order_id},
                    intent.account_id,
                )
            return
        self.store.record(
            "submitted",
            req.client_order_id,
            broker_order_id=order.broker_order_id,
            broker_status=order.status,
            filled_qty=order.filled_qty,
            filled_avg_price=order.filled_avg_price,
        )
        self.store.bump("submitted")
        item.outcome, item.detail = "submitted", f"broker status {order.status}"
        self._alert(
            report,
            "BROKER_ORDER_SUBMITTED",
            (req.client_order_id,),
            f"{req.side} {req.qty:g} {req.symbol} ({req.time_in_force})",
            f"Sandbox order submitted for risk-approved decision {intent.decision_id}.",
            {
                "symbol": req.symbol,
                "client_order_id": req.client_order_id,
                "side": req.side,
                "qty": req.qty,
                "time_in_force": req.time_in_force,
            },
            intent.account_id,
        )

    def _refresh_open_orders(self, report: SyncReport) -> None:
        """Restart recovery: resolve attempted/unknown/open orders by client_order_id."""
        if self.gateway is None:
            return
        final = {
            "filled",
            "canceled",
            "expired",
            "rejected",
            "failed",
            "blocked",
            "intent",
            "done_for_day",
        }
        for cid, o in self.store.orders().items():
            if o["status"] in final:
                continue
            try:
                order = self.gateway.order_by_client_id(cid)
            except BrokerRequestError:
                continue
            if order is None:
                if o["status"] in ("submit_attempted", "unknown"):
                    self.store.record("status", cid, broker_status="not_found_at_broker")
                continue
            if order.status != o.get("broker_status") or order.filled_qty != o.get("filled_qty"):
                self.store.record(
                    "status",
                    cid,
                    broker_order_id=order.broker_order_id,
                    broker_status=order.status,
                    filled_qty=order.filled_qty,
                    filled_avg_price=order.filled_avg_price,
                )
                report.reconciled.append(
                    {"client_order_id": cid, "status": order.status, "filled_qty": order.filled_qty}
                )
