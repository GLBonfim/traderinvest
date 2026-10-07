"""PaperTrader: the single per-session state machine used by BOTH historical replay and
incremental (restart-safe) paper trading. NOT live trading; no real-money execution exists.

For one completed session t (`process(bar)`), in order — the Phase 8/11 temporal contract:
  1. OPEN of t: the PaperBroker executes the pending risk-approved Instruction decided at the
     close of the previous session (scheduled for this session) at the raw open, Phase 8 costs;
  2. CLOSE of t: account valued at the raw close; risk monitors updated; stop evaluated on
     the close;
  3. DECISION at the close of t: strategy state -> requested target -> RiskManager -> approved
     target -> Instruction scheduled for the next XNYS session open (persisted as PENDING);
  4. snapshot of the account (with the pending kind just created).
Only the observation of bar t (`BarInput`) and state carried from earlier sessions are read.
A decision is never executed on the bar that produced it. The strategy state is never modified.
"""

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.paper.account import EPS, Account
from app.paper.broker import PaperBroker
from app.paper.config import PaperConfig
from app.paper.models import (
    PENDING,
    PENDING_ENTRY,
    PENDING_EXIT,
    PENDING_NONE,
    PENDING_REBALANCE,
    AccountSnapshot,
    Decision,
    Fill,
    Instruction,
    Order,
    PendingAction,
    Reconciliation,
    session_tag,
)
from app.risk.manager import RiskManager
from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG

TRADE_COLUMNS = (
    "trade_id",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "buy_notional",
    "sell_notional",
    "gross_pnl",
    "total_cost",
    "net_pnl",
    "net_return",
    "holding_sessions",
    "fills",
)


@dataclass(frozen=True)
class BarInput:
    """Everything the paper trader may know about completed session T, and nothing later."""

    bar_ts: pd.Timestamp  # session open (UTC), the bar's identity
    open: float
    low: float
    close: float
    strategy_state: str  # LONG | FLAT | INSUFFICIENT_DATA, as produced by the strategy
    realized_vol: float  # realized_vol_20 at T (Phase 5)
    atr: float  # atr_14 at T (Phase 5)
    observed_at: pd.Timestamp  # session close of T
    effective_at: pd.Timestamp  # next XNYS session open (calendar, not "next row")

    def fingerprint(self) -> str:
        parts = (
            self.bar_ts.isoformat(),
            repr(float(self.open)),
            repr(float(self.low)),
            repr(float(self.close)),
            self.strategy_state,
            repr(float(self.realized_vol)),
            repr(float(self.atr)),
            self.observed_at.isoformat(),
            self.effective_at.isoformat(),
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def account_id_for(
    config: PaperConfig, strategy_id: str, strategy_version: str, instrument: str, timeframe: str
) -> str:
    """Identity of a paper account: configuration + strategy + instrument, never data."""
    key = f"{config.fingerprint()}|{strategy_id}|{strategy_version}|{instrument}|{timeframe}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


@dataclass
class PaperState:
    """Complete mutable state of one paper account (serialised by `app.paper.state`)."""

    account_id: str
    strategy_id: str
    strategy_version: str
    instrument: str
    timeframe: str
    config_fingerprint: str
    account: Account
    risk: RiskManager
    broker_last_session: pd.Timestamp | None = None
    pending: Instruction | None = None
    last_requested: float = 0.0  # INSUFFICIENT_DATA keeps the previous requested target
    session_seq: int = 0  # sessions processed so far (also the next session's local index)
    last_session: pd.Timestamp | None = None
    last_decision_id: str = ""
    last_input_fingerprint: str = ""
    open_trade: dict[str, Any] | None = None
    trade_count: int = 0


@dataclass
class StepResult:
    session: pd.Timestamp
    order: Order | None
    fill: Fill | None
    reconciliation: Reconciliation | None
    decision: Decision
    instruction: Instruction
    snapshot: AccountSnapshot
    trade: dict[str, Any] | None
    executed: bool
    traded_notional: float
    costs: float
    equity: float
    exposure: float
    extra: dict[str, Any] = field(default_factory=dict)


def requested_target(state: str, previous: float) -> float:
    """Phase 8 policy (`app.backtest.portfolio.target_positions`), one bar at a time."""
    if state == LONG:
        return 1.0
    if state == FLAT:
        return 0.0
    if state == INSUFFICIENT_DATA:
        return previous
    raise ValueError(f"unknown strategy state {state!r}")


def classify_pending(target: float, account: Account, close: float, band: float) -> str:
    holding = account.holding
    if target <= EPS:
        return PENDING_EXIT if holding else PENDING_NONE
    if not holding:
        return PENDING_ENTRY
    return PENDING_REBALANCE if abs(account.exposure(close) - target) >= band else PENDING_NONE


class PaperTrader:
    """Processes completed sessions one at a time. `approved_targets` (bar_ts -> target) is
    used ONLY to replay the risk-approved targets of another run with zero costs (the gross
    shadow of a replay); it never comes from a strategy."""

    def __init__(
        self,
        config: PaperConfig,
        state: PaperState,
        *,
        approved_targets: dict[pd.Timestamp, float] | None = None,
    ) -> None:
        if state.config_fingerprint != config.fingerprint():
            raise ValueError("paper state was created with a different configuration")
        self.config = config
        self.state = state
        self.broker = PaperBroker(config, state.account)
        self.broker.last_session = state.broker_last_session
        self._approved_targets = approved_targets

    @classmethod
    def new(
        cls,
        config: PaperConfig,
        *,
        strategy_id: str,
        strategy_version: str,
        instrument: str = "SPY",
        timeframe: str = "1d",
        approved_targets: dict[pd.Timestamp, float] | None = None,
    ) -> "PaperTrader":
        capital = float(config.backtest.initial_capital)
        state = PaperState(
            account_id=account_id_for(config, strategy_id, strategy_version, instrument, timeframe),
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            instrument=instrument,
            timeframe=timeframe,
            config_fingerprint=config.fingerprint(),
            account=Account.open(capital),
            risk=RiskManager(config.risk, capital),
        )
        return cls(config, state, approved_targets=approved_targets)

    # ── one session ──

    def process(self, bar: BarInput) -> StepResult:
        st, acct, rm = self.state, self.state.account, self.state.risk
        if st.last_session is not None and bar.bar_ts <= st.last_session:
            raise ValueError("sessions must be processed in strictly increasing time order")
        if not bar.observed_at < bar.effective_at:
            raise ValueError("effective_at must be after the decision close")
        band = self.config.risk.rebalance_band
        acct_id = st.account_id
        order = fill = recon = None
        trade = None
        executed, traded, cost_today = False, 0.0, 0.0

        # 1. OPEN of t: execute the instruction decided at the previous close
        if st.pending is not None:
            ins = st.pending
            eq_open, was_flat = acct.equity(bar.open), not acct.holding
            res = self.broker.execute(
                ins,
                session_ts=bar.bar_ts,
                open_price=float(bar.open),
                band=band,
                order_id=f"{acct_id}-O{session_tag(ins.scheduled_for)}",
                fill_id=f"{acct_id}-F{session_tag(bar.bar_ts)}",
            )
            order, fill = res.order, res.fill
            if fill is not None:
                entered = was_flat and acct.holding
                if entered:
                    st.open_trade = {
                        "entry_time": bar.bar_ts,
                        "entry_seq": st.session_seq,
                        "entry_equity": eq_open,
                        "entry_price": fill.price,
                        "buy_notional": 0.0,
                        "buy_costs": 0.0,
                        "sell_notional": 0.0,
                        "sell_costs": 0.0,
                        "fills": 0,
                    }
                ot = st.open_trade
                if ot is None:
                    raise RuntimeError("fill without an open round trip")
                side = "buy" if fill.side == "buy" else "sell"
                ot[f"{side}_notional"] += fill.notional
                ot[f"{side}_costs"] += fill.total_cost
                ot["fills"] += 1
                round_trip_pnl: float | None = None
                if not acct.holding:
                    round_trip_pnl = acct.cash - ot["entry_equity"]  # Phase 11 definition
                    trade = self._close_trade(ot, bar.bar_ts, fill.price, st.session_seq)
                    st.open_trade = None
                rm.on_fill(
                    target=ins.risk_approved_target,
                    entered=entered,
                    exited=round_trip_pnl is not None,
                    price=float(bar.open),
                    atr_at_decision=ins.atr_at_decision,
                    round_trip_pnl=round_trip_pnl,
                )
                executed, traded, cost_today = True, fill.notional, fill.total_cost
            recon = self._reconcile(ins, bar, res)
            st.pending = None
        st.broker_last_session = self.broker.last_session

        # 2. CLOSE of t: valuation, monitors, close-based stop
        equity = acct.equity(bar.close)
        exposure = acct.shares * bar.close / equity
        triggered, touch = rm.on_close(
            equity, low=float(bar.low), close=float(bar.close), holding=acct.holding
        )

        # 3. DECISION at the close of t
        requested = requested_target(bar.strategy_state, st.last_requested)
        st.last_requested = requested
        if self._approved_targets is not None:
            approved = self._approved_targets[bar.bar_ts]
            reasons, intervention, risk_state = "", False, rm.risk_state
        else:
            d = rm.decide(
                requested=requested,
                close=float(bar.close),
                realized_vol=float(bar.realized_vol),
                atr=float(bar.atr),
                equity=equity,
                triggered=triggered,
                touch=touch,
            )
            approved, reasons = d.approved_exposure, ";".join(d.reasons)
            intervention, risk_state = d.intervention, d.risk_state
        kind = classify_pending(approved, acct, float(bar.close), band)
        decision_id = f"{acct_id}-D{session_tag(bar.bar_ts)}"
        input_fp = bar.fingerprint()
        decision = Decision(
            decision_id,
            acct_id,
            st.strategy_id,
            st.strategy_version,
            st.instrument,
            st.timeframe,
            bar.bar_ts,
            bar.observed_at,
            bar.strategy_state,
            requested,
            approved,
            intervention,
            reasons,
            risk_state,
            bar.effective_at,
            kind,
            st.config_fingerprint,
            input_fp,
        )
        instruction = Instruction(
            decision_id,
            bar.observed_at,
            bar.effective_at,
            requested,
            approved,
            kind,
            float(bar.atr),
        )
        st.pending = instruction

        # 4. snapshot at the close (after the decision)
        unrealized = acct.unrealized_pnl(float(bar.close))
        snapshot = AccountSnapshot(
            f"{acct_id}-S{session_tag(bar.bar_ts)}",
            bar.bar_ts,
            bar.observed_at,
            acct.cash,
            acct.shares,
            float(bar.close),
            acct.shares * bar.close,
            exposure,
            equity,
            acct.cost_basis,
            acct.realized_pnl,
            unrealized,
            acct.realized_pnl + unrealized,
            acct.cumulative_costs,
            rm.risk_state,
            kind,
        )
        st.session_seq += 1
        st.last_session = bar.bar_ts
        st.last_decision_id = decision_id
        st.last_input_fingerprint = input_fp
        return StepResult(
            bar.bar_ts,
            order,
            fill,
            recon,
            decision,
            instruction,
            snapshot,
            trade,
            executed,
            traded,
            cost_today,
            equity,
            exposure,
        )

    # ── helpers ──

    def pending_action(self) -> PendingAction | None:
        """The instruction created at the last processed close, if it requires an order."""
        ins = self.state.pending
        if ins is None or ins.pending_kind == PENDING_NONE:
            return None
        return PendingAction(
            f"{self.state.account_id}-O{session_tag(ins.scheduled_for)}",
            ins.decision_id,
            ins.pending_kind,
            "sell"
            if ins.pending_kind == PENDING_EXIT
            else ("buy" if ins.pending_kind == PENDING_ENTRY else "rebalance"),
            ins.risk_approved_target,
            ins.scheduled_for,
            PENDING,
            "scheduled session not processed yet; never executed on an invented bar",
        )

    def _close_trade(
        self, ot: dict[str, Any], exit_time: pd.Timestamp, exit_price: float, seq: int
    ) -> dict[str, Any]:
        """Round trip flat -> flat. Same formulas and operation order as the Phase 8 `Trade`
        (single entry/exit: identical values); with adds/reductions, notionals are summed."""
        st = self.state
        gross = ot["sell_notional"] - ot["buy_notional"]
        costs = ot["buy_costs"] + ot["sell_costs"]
        net = gross - costs
        committed = ot["buy_notional"] + ot["buy_costs"]
        trade = {
            "trade_id": f"{st.account_id}-T{session_tag(ot['entry_time'])}",
            "entry_time": ot["entry_time"],
            "exit_time": exit_time,
            "entry_price": ot["entry_price"],
            "exit_price": exit_price,
            "buy_notional": ot["buy_notional"],
            "sell_notional": ot["sell_notional"],
            "gross_pnl": gross,
            "total_cost": costs,
            "net_pnl": net,
            "net_return": net / committed if committed > 0 else math.nan,
            "holding_sessions": seq - ot["entry_seq"],
            "fills": ot["fills"],
        }
        st.trade_count += 1
        return trade

    def _reconcile(self, ins: Instruction, bar: BarInput, res: Any) -> Reconciliation:
        exposure = self.broker.exposure(float(bar.open))
        common = (
            bar.bar_ts,
            ins.decision_id,
            ins.strategy_requested_target,
            ins.risk_approved_target,
            ins.pending_kind,
            exposure,
        )
        if res.order is None:
            why = (
                "actual exposure within the rebalance band of the approved target"
                if res.no_order_reason == "within_rebalance_band"
                else "approved target 0 and no position"
            )
            return Reconciliation(*common, "", f"no_order_required:{res.no_order_reason}", why)
        if res.fill is None:
            return Reconciliation(
                *common,
                res.order.order_id,
                f"rejected:{res.order.rejection_reason}",
                "order rejected by broker safety check; position unchanged",
            )
        why = f"risk-approved target executed ({res.fill.action})"
        if abs(ins.strategy_requested_target - ins.risk_approved_target) > EPS:
            why += "; differs from strategy request (see decision intervention_reason)"
        return Reconciliation(*common, res.order.order_id, "filled", why)
