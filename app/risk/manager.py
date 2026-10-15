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
from app.risk.limits import (
    LOCKED,
    DrawdownMonitor,
    SessionMonitor,
    cap_exposure,
    check_position_notional,
)
from app.risk.sizing import size_exposure
from app.risk.stops import evaluate_stop, stop_level
from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG

EPS = 1e-12
STRATEGY_STATES = (LONG, FLAT, INSUFFICIENT_DATA)

# ADR-0034 D-A1: closed vocabulary of exit causes. `session_lock` is reserved (a session lock
# only blocks increases, so it never causes an exit today).
EXIT_CAUSES = ("drawdown_lock", "session_lock", "sizing", "stop", "strategy")


def canonical_exit_causes(causes: set[str] | list[str] | tuple[str, ...]) -> str:
    """Canonical `exit_causes` string: unique causes, sorted, joined by `+`."""
    unknown = set(causes) - set(EXIT_CAUSES)
    if unknown:
        raise ValueError(f"unknown exit causes: {sorted(unknown)}")
    return "+".join(sorted(set(causes)))


def validate_exit_causes(text: str) -> None:
    """Raises ValueError unless `text` is a non-empty canonical `exit_causes` string."""
    parts = text.split("+") if text else []
    if not parts:
        raise ValueError("exit_causes is empty on an exit")
    if any(p not in EXIT_CAUSES for p in parts):
        raise ValueError(f"unknown exit cause in {text!r}")
    if parts != sorted(set(parts)):
        raise ValueError(f"exit_causes {text!r} is not canonical (unique, sorted)")


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
    stop_triggered: bool  # stop CONDITION at this close (may repeat while the exit is pending)
    stop_intraday_touch: bool
    # ADR-0034 D-A1 (additive): first condition of the position episode; and, on an exit
    # decision (approved 0 while a target is held), every cause present (canonical string)
    stop_activation: bool = False
    exit_causes: str = ""


class RiskManager:
    def __init__(self, risk: RiskConfig, initial_equity: float) -> None:
        check_position_notional(risk.limits, initial_equity)
        self.risk = risk
        self.dd = DrawdownMonitor(risk.drawdown, hwm=initial_equity)
        self.sess = SessionMonitor(risk.session, last_equity=initial_equity)
        self.stop = math.nan
        self.stop_lockout = False
        # D-A1: the stop of the current position episode has activated; reset only by a
        # confirmed entry or exit fill (a pending or rejected exit keeps it)
        self.stop_activated = False
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
        strategy_state: str,
    ) -> RiskDecision:
        """Risk decision at a close. `strategy_state` (ADR-0034 D-A4, required, keyword-only) is
        the strategy's state at this close; it must agree with `requested` (LONG asks for a
        position, FLAT for none) and only FLAT releases a stop lockout."""
        if strategy_state not in STRATEGY_STATES:
            raise ValueError(f"unknown strategy state {strategy_state!r}")
        if (strategy_state == LONG and requested <= 0) or (strategy_state == FLAT and requested):
            raise ValueError(f"requested {requested!r} contradicts strategy state {strategy_state}")
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
            if approved <= EPS < sized:  # the equity grew past notional / 1e-12 (see __init__)
                raise ValueError(
                    "max_position_notional is too small for the current equity: no position "
                    "can be held (ADR-0034: no exit without a cause)"
                )
        activation = triggered and not self.stop_activated  # D-A1: first condition only
        if triggered:
            self.stop_lockout = True
            self.stop_activated = True
        if self.stop_lockout:
            if strategy_state == FLAT:  # D-A4: only a valid FLAT signal releases the lockout
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
        if equity <= 0 and approved > current:  # D-A5: nothing to size an entry from
            approved = current
            reasons.append("zero_equity_no_increase")
        if (  # D-A3: no unprotected entry (an open position keeps the level set at entry)
            risk.stop.method == "atr"
            and approved > EPS
            and current <= EPS
            and not (math.isfinite(atr) and atr > 0)
        ):
            approved = 0.0
            reasons.append("stop_input_undefined")
        exit_causes = ""
        if approved <= EPS < current:  # D-A1: an exit decision; every cause present
            causes = set()
            if requested <= EPS:
                causes.add("strategy")
            elif sized <= EPS:
                causes.add("sizing")
            # the stop stays a cause while its episode is activated and the exit is not yet
            # confirmed (pending or rejected), even if the strategy turned FLAT (which releases
            # the lockout) or the close recovered above the level
            if triggered or self.stop_lockout or self.stop_activated:
                causes.add("stop")
            if self.dd.state == LOCKED and risk.drawdown.action == "force_flat":
                causes.add("drawdown_lock")
            exit_causes = canonical_exit_causes(causes)
            validate_exit_causes(exit_causes)  # never empty on an exit
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
            stop_activation=activation,
            exit_causes=exit_causes,
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
        if entered or exited:  # D-A1: a confirmed entry or exit fill ends the episode state
            self.stop_activated = False
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
