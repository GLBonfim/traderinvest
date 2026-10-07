"""Audit records of the paper-trading replay. Every record is SIMULATED; no order is ever sent."""

from dataclasses import dataclass

import pandas as pd

# Order lifecycle (target-exposure market-on-open orders):
#   created at the scheduled open from a risk-approved instruction -> FILLED | REJECTED
#   PENDING_NO_SESSION: an entry/exit decided on the last bar with no next session in the data
FILLED, REJECTED, PENDING = "FILLED", "REJECTED", "PENDING_NO_SESSION"
ORDER_TYPE = "market_on_open_target_exposure"


@dataclass(frozen=True)
class Decision:
    decision_id: str
    strategy_id: str
    strategy_version: str
    instrument: str
    bar_ts: pd.Timestamp
    observed_at: pd.Timestamp  # close of T: when the decision is made
    strategy_state: str  # copied verbatim, never modified
    strategy_requested_target: float
    risk_approved_target: float
    intervention: bool
    intervention_reason: str
    risk_state: str
    scheduled_execution_at: pd.Timestamp  # next XNYS session open
    config_fingerprint: str
    data_fingerprint: str


@dataclass(frozen=True)
class Instruction:
    """What reaches the broker: the RISK-APPROVED target only (never the raw strategy state)."""

    decision_id: str
    observed_at: pd.Timestamp
    scheduled_for: pd.Timestamp
    strategy_requested_target: float
    risk_approved_target: float


@dataclass(frozen=True)
class Order:
    order_id: str
    decision_id: str
    order_type: str
    side: str
    requested_quantity: float  # shares to reach the strategy-requested target at the open (info)
    approved_quantity: float  # shares to reach the risk-approved target (what is sent)
    scheduled_execution_at: pd.Timestamp
    status: str
    rejection_reason: str


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    executed_at: pd.Timestamp
    price: float  # raw session open (Phase 8 convention)
    quantity: float
    notional: float
    commission: float
    spread_cost: float
    slippage_cost: float
    total_cost: float


@dataclass(frozen=True)
class PortfolioSnapshot:
    ts: pd.Timestamp  # bar ts; valued at the session close
    cash: float
    position_quantity: float
    position_market_value: float
    gross_exposure: float
    equity: float
    realized_pnl: float  # average-cost method, gross of costs
    unrealized_pnl: float  # (close - average cost) x quantity, gross of costs
    cumulative_costs: float
    risk_state: str


@dataclass(frozen=True)
class Reconciliation:
    """Per execution point: what was requested, approved and actually held after the open."""

    ts: pd.Timestamp
    decision_id: str
    strategy_requested_target: float
    risk_approved_target: float
    executed_exposure_at_open: float  # position after the open / equity at the open
    order_id: str
    outcome: str  # filled | rejected:<reason> | no_order_required:<why>
    explanation: str
