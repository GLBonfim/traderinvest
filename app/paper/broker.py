"""PaperBroker: an internal, deterministic execution simulator. No network, no credentials,
no live orders — there is no code path that could send an order anywhere.

It accepts only risk-approved `Instruction`s and fills them at the raw OPEN of their scheduled
session using the Phase 8 cost model (via `app.risk.manager.plan_order/apply_order`, the same
functions the Phase 11 simulation uses). Hard safety checks are applied independently of the
risk layer; a failing check REJECTS the order with an explicit reason (no fallback).
"""

from dataclasses import dataclass

import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.execution import order_costs
from app.paper.config import PaperConfig
from app.paper.models import FILLED, ORDER_TYPE, REJECTED, Fill, Instruction, Order
from app.risk.manager import EPS, PlannedOrder, apply_order, plan_order


@dataclass
class ExecutionResult:
    order: Order | None
    fill: Fill | None
    planned: PlannedOrder | None
    no_order_reason: str  # set when no order was required


class PaperBroker:
    mode = "paper_simulation"

    def __init__(self, config: PaperConfig) -> None:
        self.config = config
        self.bt: BacktestConfig = config.backtest
        self.cash = float(self.bt.initial_capital)
        self.shares = 0.0
        self.avg_cost = 0.0
        self.realized_pnl = 0.0
        self.cumulative_costs = 0.0
        self._last_session: pd.Timestamp | None = None
        self._orders = 0

    # ── state ──

    def equity(self, price: float) -> float:
        return self.cash + self.shares * price

    def exposure(self, price: float) -> float:
        eq = self.equity(price)
        return self.shares * price / eq if eq > 0 else 0.0

    # ── execution ──

    def execute(
        self,
        instruction: Instruction,
        *,
        session_ts: pd.Timestamp,
        open_price: float,
        band: float,
        id_prefix: str,
    ) -> ExecutionResult:
        """Processes one instruction at the open of `session_ts`."""
        if not isinstance(instruction, Instruction):
            raise TypeError("PaperBroker accepts only risk-approved Instructions, never raw states")
        if self._last_session is not None and session_ts <= self._last_session:
            raise ValueError("sessions must be processed in strictly increasing time order")
        self._last_session = session_ts

        planned = plan_order(
            instruction.risk_approved_target,
            cash=self.cash,
            shares=self.shares,
            price=open_price,
            band=band,
            bt=self.bt,
        )
        if planned is None:
            holding = self.shares > EPS
            reason = "within_rebalance_band" if holding else "already_flat"
            return ExecutionResult(None, None, None, reason)

        self._orders += 1
        order_id = f"{id_prefix}-O{self._orders:06d}"
        approved_qty = planned.notional / open_price * (1 if planned.side == "buy" else -1)
        eq_open = self.equity(open_price)
        requested_qty = instruction.strategy_requested_target * eq_open / open_price - self.shares

        def order(status: str, reason: str = "") -> Order:
            return Order(
                order_id,
                instruction.decision_id,
                ORDER_TYPE,
                planned.side,
                requested_qty,
                approved_qty,
                instruction.scheduled_for,
                status,
                reason,
            )

        reason = self._safety_check(instruction, planned, session_ts, open_price)
        if reason:
            return ExecutionResult(order(REJECTED, reason), None, planned, "")

        before_shares, before_avg = self.shares, self.avg_cost
        self.cash, self.shares, costs = apply_order(
            planned, cash=self.cash, shares=self.shares, price=open_price, bt=self.bt
        )
        qty = self.shares - before_shares
        if qty > 0:
            self.avg_cost = (before_avg * before_shares + planned.notional) / self.shares
        else:
            self.realized_pnl += (open_price - before_avg) * (-qty)
            if self.shares <= EPS:
                self.avg_cost = 0.0
        self.cumulative_costs += costs.total
        fill = Fill(
            f"{id_prefix}-F{self._orders:06d}",
            order_id,
            session_ts,
            open_price,
            qty,
            planned.notional,
            costs.commission,
            costs.spread,
            costs.slippage,
            costs.total,
        )
        return ExecutionResult(order(FILLED), fill, planned, "")

    def _safety_check(
        self, ins: Instruction, planned: PlannedOrder, session_ts: pd.Timestamp, price: float
    ) -> str:
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
        if planned.side == "sell" and planned.notional > self.shares * price * (1 + 1e-12):
            return "short_not_allowed"
        if planned.side == "buy":
            cash_after = self.cash - (
                planned.notional + order_costs(planned.notional, self.bt).total
            )
            if cash_after < -self.config.cash_tolerance:
                return "insufficient_cash"
            position_after = self.shares * price + planned.notional
            if (
                position_after / (cash_after + position_after)
                > self.config.max_gross_exposure + 1e-9
            ):
                return "exceeds_max_gross_exposure"  # leverage would be required
        return ""
