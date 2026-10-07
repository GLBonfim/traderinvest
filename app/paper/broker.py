"""PaperBroker: an internal, deterministic execution simulator. No network, no credentials,
no live orders — there is no code path that could send an order anywhere.

It accepts only risk-approved `Instruction`s and fills them at the raw OPEN of their scheduled
session using the Phase 8 cost model (via `app.risk.manager.plan_order/apply_order`, the same
functions the Phase 11 simulation uses). Hard safety checks are applied independently of the
risk layer; a failing check REJECTS the order with an explicit reason (no fallback).

The only difference from the Phase 8/11 arithmetic: `buy_notional` solves N + costs(N) = cash
in floating point and can leave cash at about -1e-11 (and exposure 1 + 2e-16). The paper
broker never books negative cash: it lowers a buy's notional by a few ulps until the resulting
cash is >= 0, at most `cash_tolerance` (currency) in total; beyond that the order is rejected
(`insufficient_cash`). Effect on equity: ~1e-11 of the account currency per entry.
"""

import math
from dataclasses import dataclass

import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.execution import order_costs
from app.paper.account import EPS, Account
from app.paper.config import PaperConfig
from app.paper.models import (
    ADD,
    ENTRY,
    EXIT,
    FILLED,
    ORDER_TYPE,
    REDUCE,
    REJECTED,
    Fill,
    Instruction,
    Order,
)
from app.risk.manager import PlannedOrder, apply_order, plan_order


@dataclass
class ExecutionResult:
    order: Order | None
    fill: Fill | None
    planned: PlannedOrder | None
    no_order_reason: str  # set when no order was required


class PaperBroker:
    mode = "paper_simulation"

    def __init__(self, config: PaperConfig, account: Account | None = None) -> None:
        self.config = config
        self.bt: BacktestConfig = config.backtest
        self.account = account or Account.open(float(self.bt.initial_capital))
        self.last_session: pd.Timestamp | None = None

    # ── state (read-only views of the account) ──

    @property
    def cash(self) -> float:
        return self.account.cash

    @property
    def shares(self) -> float:
        return self.account.shares

    def equity(self, price: float) -> float:
        return self.account.equity(price)

    def exposure(self, price: float) -> float:
        return self.account.exposure(price)

    # ── execution ──

    def execute(
        self,
        instruction: Instruction,
        *,
        session_ts: pd.Timestamp,
        open_price: float,
        band: float,
        order_id: str,
        fill_id: str,
    ) -> ExecutionResult:
        """Processes one instruction at the open of `session_ts`."""
        if not isinstance(instruction, Instruction):
            raise TypeError("PaperBroker accepts only risk-approved Instructions, never raw states")
        if self.last_session is not None and session_ts <= self.last_session:
            raise ValueError("sessions must be processed in strictly increasing time order")
        self.last_session = session_ts
        acct = self.account

        planned = plan_order(
            instruction.risk_approved_target,
            cash=acct.cash,
            shares=acct.shares,
            price=open_price,
            band=band,
            bt=self.bt,
        )
        if planned is None:
            reason = "within_rebalance_band" if acct.holding else "already_flat"
            return ExecutionResult(None, None, None, reason)
        if planned.side == "buy":
            planned = self._without_negative_cash(planned)

        approved_qty = planned.notional / open_price * (1 if planned.side == "buy" else -1)
        eq_open = acct.equity(open_price)
        requested_qty = instruction.strategy_requested_target * eq_open / open_price - acct.shares
        if planned.side == "buy":
            action = ENTRY if not acct.holding else ADD
        else:
            action = EXIT if planned.exit_all else REDUCE

        def order(status: str, reason: str = "") -> Order:
            return Order(
                order_id,
                instruction.decision_id,
                ORDER_TYPE,
                planned.side,
                action,
                requested_qty,
                approved_qty,
                instruction.scheduled_for,
                status,
                reason,
            )

        reason = self._safety_check(instruction, planned, session_ts, open_price)
        if reason:
            return ExecutionResult(order(REJECTED, reason), None, planned, "")

        before = acct.shares
        cash, shares, costs = apply_order(
            planned, cash=acct.cash, shares=acct.shares, price=open_price, bt=self.bt
        )
        booked, realized = acct.apply_fill(
            side=planned.side,
            notional=planned.notional,
            costs=costs,
            new_cash=cash,
            new_shares=shares,
        )
        if booked != action:  # defensive: accounting and order classification must agree
            raise RuntimeError(f"fill booked as {booked!r} but ordered as {action!r}")
        fill = Fill(
            fill_id,
            order_id,
            session_ts,
            planned.side,
            action,
            open_price,
            shares - before,
            planned.notional,
            costs.commission,
            costs.spread,
            costs.slippage,
            costs.total,
            realized,
            acct.cash,
            acct.shares,
        )
        return ExecutionResult(order(FILLED), fill, planned, "")

    def _without_negative_cash(self, planned: PlannedOrder) -> PlannedOrder:
        """Largest notional <= the planned one (within `cash_tolerance`) leaving cash >= 0, using
        exactly the expression `apply_order` books."""
        cash, n = self.account.cash, planned.notional
        while cash - (n + order_costs(n, self.bt).total) < 0.0:
            n = math.nextafter(n, 0.0)
            if planned.notional - n > self.config.cash_tolerance or n <= 0:
                return planned  # not a rounding residue: the safety check rejects it
        return planned if n == planned.notional else PlannedOrder("buy", n, False)

    def _safety_check(
        self, ins: Instruction, planned: PlannedOrder, session_ts: pd.Timestamp, price: float
    ) -> str:
        acct = self.account
        if session_ts != ins.scheduled_for:
            return "scheduled_session_missing_in_data"
        if session_ts <= ins.observed_at:
            return "execution_not_after_decision"
        if ins.risk_approved_target < 0:
            return "short_not_allowed"
        if ins.risk_approved_target > self.config.max_gross_exposure + EPS:
            return "exceeds_max_gross_exposure"
        if planned.notional < 0:
            return "negative_quantity"
        if planned.side == "sell" and planned.notional > acct.shares * price * (1 + 1e-12):
            return "short_not_allowed"
        if planned.side == "buy":
            cash_after = acct.cash - (
                planned.notional + order_costs(planned.notional, self.bt).total
            )
            if cash_after < 0.0:
                return "insufficient_cash"
            position_after = acct.shares * price + planned.notional
            if (
                position_after / (cash_after + position_after)
                > self.config.max_gross_exposure + 1e-9
            ):
                return "exceeds_max_gross_exposure"  # leverage would be required
        return ""
