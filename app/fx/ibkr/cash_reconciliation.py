"""Explicit PAPER cash-ledger reconciliation when virtual FX positions are unavailable.

The anchor is an independently audited flat strategy account, NOT a replacement funding
order. Every later execution must belong to a journaled intent. Cash changes, commissions
and signed pair units are recorded atomically in a hash-chained store. Missing executions,
unknown trades, corrections, cash transfers, or non-FX exposure fail closed.
"""

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.fx.ibkr.paper import assert_paper_accounts
from app.fx.orders import ExecutionError
from app.fx.store import FxStore

PAIRS = frozenset({"EURUSD", "EURGBP", "EURJPY"})
KEY = "cash_reconciliation"


class CommissionPendingError(ExecutionError):
    def __init__(self) -> None:
        super().__init__("execution commission has not arrived", ambiguous=True)


def state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def cash_balances(ib: Any) -> dict[str, float]:
    result: dict[str, float] = {}
    for v in ib.accountValues():
        if v.tag == "$LEDGER-CashBalance" and v.currency not in ("", "BASE"):
            amount = float(v.value)
            if not math.isfinite(amount) or v.currency in result:
                raise ExecutionError("ambiguous or non-finite currency cash", ambiguous=True)
            result[v.currency] = amount
    return result


def compare_cash(expected: dict[str, float], actual: dict[str, float]) -> bool:
    if not {"EUR", "USD", "GBP", "JPY"} <= set(actual):
        return False
    return all(
        math.isfinite(actual.get(c, 0.0))
        and abs(actual.get(c, 0.0) - expected.get(c, 0.0)) <= (0.5 if c == "JPY" else 0.005)
        for c in set(expected) | set(actual)
    )


class CashReconciler:
    def __init__(self, ib: Any, store: FxStore, anchor_path: Path, funding_path: Path) -> None:
        self.ib, self.store = ib, store
        self.anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        self.anchor_hash = hashlib.sha256(anchor_path.read_bytes()).hexdigest()
        if self.anchor.get("schema") != "fx-paper-cash-anchor/1":
            raise ValueError("unsupported cash anchor")
        if hashlib.sha256(funding_path.read_bytes()).hexdigest() != self.anchor["funding_sha256"]:
            raise ValueError("funding changed after cash anchor")
        if not store.verify()[0]:
            raise ExecutionError("pilot journal is not intact", ambiguous=True)
        saved = store.get_state(KEY)
        self.state = saved or {
            "anchor_sha256": self.anchor_hash,
            "cash": self.anchor["cash"],
            "positions": {},
            "fills": {},
        }
        if self.state["anchor_sha256"] != self.anchor_hash:
            raise ExecutionError(
                "cash anchor changed: manual reconciliation required", ambiguous=True
            )
        self.verify_account()
        if saved is None:
            with store.transaction():
                store.append(
                    "cash_anchor_bound",
                    {"anchor_sha256": self.anchor_hash, "state_sha256": state_digest(self.state)},
                )
                store.put_state(KEY, self.state)
        self.verify_state()

    def verify_state(self) -> None:
        recent = self.store.recent(1, ("cash_anchor_bound", "cash_execution_batch"))
        if not recent or recent[0]["payload"].get("state_sha256") != state_digest(self.state):
            raise ExecutionError("cash state differs from journal", ambiguous=True)

    def verify_account(self) -> None:
        ids = self.ib.managedAccounts()
        accounts = assert_paper_accounts(ids)
        if len(accounts) != 1 or accounts[0] != self.anchor["account"]:
            raise ExecutionError("cash anchor belongs to another paper account", ambiguous=True)

    def known_refs(self) -> set[str]:
        refs: set[str] = set()
        for (body,) in self.store.db.execute(
            "SELECT payload FROM journal WHERE kind IN "
            "('pilot_order_sent','user_demo_test_intent','user_demo_test_close_intent')"
        ):
            payload = json.loads(body)
            coid = payload.get("coid", payload.get("ref", ""))
            if coid.startswith(("P-", "USER-DEMO-")):
                refs.update((coid, coid + ":sl", coid + ":tp", coid + ":close"))
        return refs

    def apply_fills(self, fills: list[Any]) -> None:
        state = json.loads(json.dumps(self.state))
        refs = self.known_refs()
        new: list[dict[str, Any]] = []
        cached_fills = getattr(self.ib, "fills", None)
        reports = {
            f.execution.execId: f.commissionReport
            for f in (cached_fills() if callable(cached_fills) else [])
        }
        for fill in fills:
            e, c, commission = fill.execution, fill.contract, fill.commissionReport
            fid = str(e.execId)
            if fid in self.anchor["prior_exec_ids"]:
                continue
            # ib_async returns new Fill objects from reqExecutionsAsync; commission
            # callbacks update its public fills() cache, not those returned copies.
            if commission.execId != fid or not commission.currency:
                commission = reports.get(fid, commission)
            sym = c.symbol + c.currency
            if (
                c.secType != "CASH"
                or sym not in PAIRS
                or e.orderRef not in refs
                or e.acctNumber != self.anchor["account"]
                or e.side not in ("BOT", "SLD")
            ):
                raise ExecutionError(
                    "unknown execution: cash reconciliation paused", ambiguous=True
                )
            qty, price, fee = float(e.shares), float(e.price), float(commission.commission)
            if commission.execId != fid or not commission.currency:
                raise CommissionPendingError()
            if (
                not fid
                or commission.execId != fid
                or commission.currency not in {"EUR", "USD", "GBP", "JPY"}
                or not all(math.isfinite(v) for v in (qty, price, fee))
                or qty <= 0
                or price <= 0
                or fee < 0
                or fee > 100
            ):
                raise ExecutionError("execution or commission not confirmed", ambiguous=True)
            item = {
                "id": fid,
                "ref": e.orderRef,
                "pair": sym,
                "side": e.side,
                "units": qty,
                "price": price,
                "fee": fee,
                "fee_currency": commission.currency,
            }
            if fid in state["fills"]:
                if state["fills"][fid] != item:
                    raise ExecutionError("execution correction requires review", ambiguous=True)
                continue
            sign = 1 if e.side == "BOT" else -1
            cash = state["cash"]
            cash["EUR"] += sign * qty
            cash[sym[3:]] -= sign * qty * price
            cash[commission.currency] -= fee
            state["positions"][sym] = state["positions"].get(sym, 0.0) + sign * qty
            state["fills"][fid] = item
            new.append(item)
        if new:
            with self.store.transaction():
                self.store.append(
                    "cash_execution_batch", {"fills": new, "state_sha256": state_digest(state)}
                )
                self.store.put_state(KEY, state)
            self.state = state

    async def __call__(self) -> dict[str, float]:
        self.verify_account()
        if not self.store.verify()[0]:
            raise ExecutionError("pilot journal is not intact", ambiguous=True)
        self.verify_state()
        # Read all-account orders to detect interference from another API client or the UI.
        refs = self.known_refs()
        orders = await asyncio.wait_for(self.ib.reqAllOpenOrdersAsync(), 15)
        if any(t.order.orderRef not in refs for t in orders):
            raise ExecutionError(
                "external working order: cash reconciliation paused", ambiguous=True
            )
        positions = await asyncio.wait_for(self.ib.reqPositionsAsync(), 15)
        if any(
            p.position
            and (
                p.contract.secType != "CASH" or p.contract.symbol + p.contract.currency not in PAIRS
            )
            for p in positions
        ):
            raise ExecutionError(
                "external asset position: cash reconciliation paused", ambiguous=True
            )
        from ib_async import ExecutionFilter

        for attempt in range(4):
            fills = await asyncio.wait_for(self.ib.reqExecutionsAsync(ExecutionFilter()), 15)
            ready = True
            try:
                self.apply_fills(fills)
            except CommissionPendingError:
                ready = False
            if ready and compare_cash(self.state["cash"], cash_balances(self.ib)):
                self.verify_account()
                return {s: u for s, u in self.state["positions"].items() if abs(u) > 1e-8}
            if attempt < 3:
                await asyncio.sleep(1)
        raise ExecutionError("cash does not match confirmed executions: paused", ambiguous=True)

    def status(self) -> dict[str, Any]:
        return {
            "mode": "cash_and_confirmed_executions",
            "anchor_sha256": self.anchor_hash,
            "tracked_fills": len(self.state["fills"]),
            "checked_at": datetime.now(UTC).isoformat(),
        }
