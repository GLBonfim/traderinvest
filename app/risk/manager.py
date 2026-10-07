"""Stateful risk manager and order planning — the single source of truth for Phase 11 risk
decisions, used step by step by both the risk simulation (`app.risk.engine`) and the paper
broker (`app.paper`).

Extracted from `app.risk.engine.simulate` in Phase 12 as a pure refactor: the decision order
and every formula are unchanged (verified bit-exact against the Phase 11 outputs).

Lifecycle per session t (driven by the caller):
  1. at the open of t: `plan_order` for the target decided at t-1; if an order is filled the
     caller reports it through `on_fill`;
  2. at the close of t: `on_close(equity, low, close, holding)` -> (stop triggered, touch);
  3. `decide(...)` with information known at the close of t -> RiskDecision.
"""

import math
from dataclasses import dataclass

from app.backtest.config import BacktestConfig
from app.backtest.execution import buy_notional, order_costs
from app.backtest.models import Costs
from app.risk.config import RiskConfig
from app.risk.limits import LOCKED, DrawdownMonitor, SessionMonitor, cap_exposure
from app.risk.sizing import size_exposure
from app.risk.stops import evaluate_stop, stop_level

EPS = 1e-12


@dataclass(frozen=True)
class RiskDecision:
    requested_exposure: float  # what the strategy asked for (LONG 1 / FLAT 0)
    sized_exposure: float
    approved_exposure: float  # what the risk layer permits (this is the broker's target)
    intervention: bool  # approved != requested
    reasons: tuple[str, ...]
    risk_state: str
    drawdown: float
    high_water_mark: float
    stop_level: float
    stop_triggered: bool
    stop_intraday_touch: bool


class RiskManager:
    def __init__(self, risk: RiskConfig, initial_equity: float) -> None:
        self.risk = risk
        self.dd = DrawdownMonitor(risk.drawdown, hwm=initial_equity)
        self.sess = SessionMonitor(risk.session, last_equity=initial_equity)
        self.stop = math.nan
        self.stop_lockout = False
        self.current_target = 0.0  # last EXECUTED target

    @property
    def risk_state(self) -> str:
        return LOCKED if (self.dd.state == LOCKED or self.sess.locked) else self.dd.state

    def on_close(
        self, equity: float, *, low: float, close: float, holding: bool
    ) -> tuple[bool, bool]:
        self.dd.update(equity)
        self.sess.update(equity)
        return evaluate_stop(self.stop, low=low, close=close) if holding else (False, False)

    def decide(
        self,
        *,
        requested: float,
        close: float,
        realized_vol: float,
        atr: float,
        equity: float,
        triggered: bool,
        touch: bool,
    ) -> RiskDecision:
        risk, current = self.risk, self.current_target
        reasons: list[str] = []
        if requested > 0:
            sized, why = size_exposure(risk.sizing, close=close, realized_vol=realized_vol, atr=atr)
            if why == "risk_input_undefined":
                reasons.append(why)
            elif sized < requested:
                reasons.append(f"sizing_{why}")
        else:
            sized = 0.0
        approved, lim = cap_exposure(sized, risk.limits, equity=equity)
        if lim:
            reasons.append(lim)
        if triggered:
            self.stop_lockout = True
        if self.stop_lockout:
            if requested == 0:
                self.stop_lockout = False  # strategy signal reset: re-entry allowed again
            elif approved > 0:
                approved = 0.0
                reasons.append("stop_triggered" if triggered else "stop_lockout_until_signal_reset")
        if self.dd.state == LOCKED:
            if risk.drawdown.action == "force_flat" and approved > 0:
                approved = 0.0
                reasons.append("drawdown_lock_flat")
            elif approved > current:
                approved = current
                reasons.append("drawdown_lock_no_increase")
        if self.sess.locked and approved > current:
            approved = current
            reasons.append("session_lock_no_increase")
        return RiskDecision(
            requested_exposure=requested,
            sized_exposure=sized,
            approved_exposure=approved,
            intervention=bool(abs(approved - requested) > EPS),
            reasons=tuple(reasons),
            risk_state=self.risk_state,
            drawdown=equity / self.dd.hwm - 1 if self.dd.hwm > 0 else math.nan,
            high_water_mark=self.dd.hwm,
            stop_level=self.stop,
            stop_triggered=triggered,
            stop_intraday_touch=touch,
        )

    def on_fill(
        self,
        *,
        target: float,
        entered: bool,
        exited: bool,
        price: float,
        atr_at_decision: float,
        round_trip_pnl: float | None,
    ) -> None:
        if entered:
            self.stop = stop_level(
                self.risk.stop, entry_price=price, atr_at_decision=atr_at_decision
            )
        if exited:
            if round_trip_pnl is not None:
                self.sess.record_round_trip(round_trip_pnl)
            self.stop = math.nan
        self.current_target = target


@dataclass(frozen=True)
class PlannedOrder:
    side: str  # "buy" | "sell"
    notional: float
    exit_all: bool


def plan_order(
    target: float, *, cash: float, shares: float, price: float, band: float, bt: BacktestConfig
) -> PlannedOrder | None:
    """Order needed at an open to move from the current position to `target` exposure.
    Trade on entry/exit, or when |actual exposure - target| >= band; otherwise None."""
    eq_open = cash + shares * price
    actual = shares * price / eq_open if eq_open > 0 else 0.0
    enter_or_exit = (target <= EPS) != (shares <= EPS)
    if not (enter_or_exit or (target > EPS and abs(actual - target) >= band)):
        return None
    if target <= EPS:
        return PlannedOrder("sell", shares * price, True)
    desired, value = target * eq_open, shares * price
    if desired > value:
        notional = min(desired - value, buy_notional(cash, bt)) if cash > 0 else 0.0
        return PlannedOrder("buy", notional, False)
    return PlannedOrder("sell", value - desired, False)


def apply_order(order: PlannedOrder, *, cash: float, shares: float, price: float,
                bt: BacktestConfig) -> tuple[float, float, Costs]:  # fmt: skip
    """New (cash, shares) after filling `order` at `price`, with Phase 8 costs."""
    costs = order_costs(order.notional, bt)
    if order.side == "buy":
        return cash - (order.notional + costs.total), shares + order.notional / price, costs
    new_shares = 0.0 if order.exit_all else shares - order.notional / price
    return cash + (order.notional - costs.total), new_shares, costs
