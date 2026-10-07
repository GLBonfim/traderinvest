"""Audit records of paper trading. Every record is SIMULATED; no order is ever sent anywhere.

Identity: every record ID is built only from information available when the record is created
(account ID + record type + session date), never from a fingerprint of the whole dataset, so
appending or changing bars after T never renames a record created at or before T.

    account_id   = sha256(paper config fingerprint | strategy | strategy version | instrument |
                   timeframe)[:12]
    decision_id  = <account_id>-D<YYYYMMDD of the decision bar>
    order_id     = <account_id>-O<YYYYMMDD of the scheduled execution session>
    fill_id      = <account_id>-F<YYYYMMDD of the execution session>
    snapshot_id  = <account_id>-S<YYYYMMDD of the valued bar>
At most one decision, one order and one fill exist per account and session.
"""

from dataclasses import dataclass

import pandas as pd

# Order lifecycle (target-exposure market-on-open orders):
#   created at the scheduled open from a risk-approved instruction -> FILLED | REJECTED
#   PENDING_NO_SESSION: an instruction requiring an order, decided on the last processed bar
#   (its execution session is not in the data yet; never executed on an invented bar)
FILLED, REJECTED, PENDING = "FILLED", "REJECTED", "PENDING_NO_SESSION"
ORDER_TYPE = "market_on_open_target_exposure"

# Pending kinds, classified at the decision close from the position held at that close:
#   none      : no order expected (flat and target 0, or holding within the rebalance band)
#   entry     : flat -> target > 0
#   exit      : holding -> target 0
#   rebalance : holding -> target > 0 with |exposure at close - target| >= band
# The open of the next session decides what actually happens (prices move overnight); the
# reconciliation record states the actual outcome.
PENDING_NONE, PENDING_ENTRY, PENDING_EXIT, PENDING_REBALANCE = "none", "entry", "exit", "rebalance"

# Fill actions: entry (flat -> long), add (buy while long), reduce (partial sell), exit (to flat)
ENTRY, ADD, REDUCE, EXIT = "entry", "add", "reduce", "exit"


def session_tag(ts: pd.Timestamp) -> str:
    return f"{pd.Timestamp(ts).tz_convert('America/New_York'):%Y%m%d}"


@dataclass(frozen=True)
class Decision:
    decision_id: str
    account_id: str
    strategy_id: str
    strategy_version: str
    instrument: str
    timeframe: str
    bar_ts: pd.Timestamp
    observed_at: pd.Timestamp  # close of T: when the decision is made
    strategy_state: str  # copied verbatim, never modified
    strategy_requested_target: float
    risk_approved_target: float
    intervention: bool
    intervention_reason: str
    risk_state: str
    scheduled_execution_at: pd.Timestamp  # next XNYS session open
    pending_kind: str
    config_fingerprint: str
    input_fingerprint: str  # hash of the observation of bar T only (point-in-time)


@dataclass(frozen=True)
class Instruction:
    """What reaches the broker: the RISK-APPROVED target only (never the raw strategy state)."""

    decision_id: str
    observed_at: pd.Timestamp
    scheduled_for: pd.Timestamp
    strategy_requested_target: float
    risk_approved_target: float
    pending_kind: str
    atr_at_decision: float  # ATR known at the decision close (sets an ATR stop on entry)


@dataclass(frozen=True)
class Order:
    order_id: str
    decision_id: str
    order_type: str
    side: str
    action: str  # entry | add | reduce | exit
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
    side: str
    action: str
    price: float  # raw session open (Phase 8 convention)
    quantity: float  # signed: + bought, - sold
    notional: float
    commission: float
    spread_cost: float
    slippage_cost: float
    total_cost: float
    realized_pnl: float  # realized by this fill (0 for buys); see docs/paper-trading.md
    cash_after: float
    position_after: float


@dataclass(frozen=True)
class AccountSnapshot:
    snapshot_id: str
    ts: pd.Timestamp  # bar ts (session open, UTC)
    valued_at: pd.Timestamp  # session close: the mark time
    cash: float
    position_quantity: float
    mark_price: float  # raw close of the session
    position_market_value: float
    gross_exposure: float
    equity: float
    cost_basis: float  # notional + costs of the shares held (average-cost method)
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float  # realized + unrealized
    cumulative_costs: float
    risk_state: str
    pending_kind: str  # pending action created at this close


@dataclass(frozen=True)
class Reconciliation:
    """Per execution point: what was requested, approved and actually held after the open."""

    ts: pd.Timestamp
    decision_id: str
    strategy_requested_target: float
    risk_approved_target: float
    pending_kind: str
    executed_exposure_at_open: float  # position after the open / equity at the open
    order_id: str
    outcome: str  # filled | rejected:<reason> | no_order_required:<why>
    explanation: str


@dataclass(frozen=True)
class PendingAction:
    """An instruction decided on the last processed bar whose session is not in the data."""

    order_id: str
    decision_id: str
    kind: str  # entry | exit | rebalance
    side: str
    risk_approved_target: float
    scheduled_execution_at: pd.Timestamp
    status: str
    note: str
