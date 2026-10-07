"""Risk configuration and the PRE-DECLARED research scenarios (ADR-0020).

Every value is a conventional choice fixed before any result was seen. None was tuned on
historical performance, and none may be tuned on the Phase 10 test results.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field

RISK_VERSION = "1.0.0"
SIZING_METHODS = ("full", "fixed_fraction", "volatility_target", "atr_risk")
STOP_METHODS = ("none", "fixed_pct", "atr")
LOCK_ACTIONS = ("force_flat", "block_entries")


@dataclass(frozen=True)
class SizingSpec:
    """Exposure = fraction of current equity held in the instrument when the strategy is LONG.

    full              : 1.0
    fixed_fraction    : `fraction`
    volatility_target : target_volatility / realized_vol_20(T)      (both annualised)
    atr_risk          : atr_risk_budget * close(T) / (atr_stop_multiple * ATR14(T))
                        (loss of `atr_risk_budget` x equity if price falls by k x ATR)
    All capped at `max_exposure` (<= 1: no leverage).
    """

    method: str = "full"
    fraction: float = 1.0
    target_volatility: float = 0.10
    atr_risk_budget: float = 0.01
    atr_stop_multiple: float = 2.0
    max_exposure: float = 1.0


@dataclass(frozen=True)
class StopSpec:
    """Long stop below the entry fill. Evaluated on the CLOSE (close <= level) -> exit at the
    next session open. Intraday stop orders are unsupported (no intraday data)."""

    method: str = "none"
    pct: float = 0.10
    atr_multiple: float = 3.0


@dataclass(frozen=True)
class DrawdownSpec:
    """drawdown = equity / high-water mark - 1 (HWM includes initial capital), at the close.
    WARNING when drawdown <= -warning; LOCKED when <= -lock. While LOCKED: `force_flat` exits
    and blocks entries, `block_entries` keeps the position but blocks increases. The lock
    lasts `cooldown_sessions` decisions, then unlocks and resets the HWM to current equity."""

    enabled: bool = False
    warning: float = 0.10
    lock: float = 0.20
    action: str = "force_flat"
    cooldown_sessions: int = 21


@dataclass(frozen=True)
class SessionSpec:
    """Session loss = equity(close T) / equity(close T-1) - 1. If <= -max_session_loss, entries
    and increases are blocked for `lock_sessions` decisions. Consecutive losing round trips
    >= max_consecutive_losses (if set) block entries for `consecutive_lock_sessions`."""

    enabled: bool = False
    max_session_loss: float = 0.03
    lock_sessions: int = 1
    max_consecutive_losses: int | None = None
    consecutive_lock_sessions: int = 5


@dataclass(frozen=True)
class LimitSpec:
    max_gross_exposure: float = 1.0
    max_single_instrument_exposure: float = 1.0
    max_position_notional: float | None = None  # absolute currency cap, optional


@dataclass(frozen=True)
class RiskConfig:
    name: str = "control_no_overlay"
    sizing: SizingSpec = field(default_factory=SizingSpec)
    stop: StopSpec = field(default_factory=StopSpec)
    drawdown: DrawdownSpec = field(default_factory=DrawdownSpec)
    session: SessionSpec = field(default_factory=SessionSpec)
    limits: LimitSpec = field(default_factory=LimitSpec)
    # an open position is re-sized at the next open only if |actual exposure - target| >= band
    rebalance_band: float = 0.10

    def __post_init__(self) -> None:
        s, st, dd, ss, lim = self.sizing, self.stop, self.drawdown, self.session, self.limits
        if s.method not in SIZING_METHODS:
            raise ValueError(f"sizing.method must be one of {SIZING_METHODS}")
        if not 0 < s.max_exposure <= 1 or not 0 < s.fraction <= 1:
            raise ValueError("exposures must be in (0, 1] (no leverage)")
        if s.target_volatility <= 0 or s.atr_risk_budget <= 0 or s.atr_stop_multiple <= 0:
            raise ValueError("sizing parameters must be > 0")
        if st.method not in STOP_METHODS or not 0 < st.pct < 1 or st.atr_multiple <= 0:
            raise ValueError("invalid stop specification")
        if (
            dd.action not in LOCK_ACTIONS
            or not 0 < dd.warning < dd.lock < 1
            or dd.cooldown_sessions < 1
        ):
            raise ValueError("invalid drawdown specification")
        if (
            not 0 < ss.max_session_loss < 1
            or ss.lock_sessions < 1
            or ss.consecutive_lock_sessions < 1
        ):
            raise ValueError("invalid session specification")
        if ss.max_consecutive_losses is not None and ss.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be >= 1")
        if not 0 < lim.max_gross_exposure <= 1 or not 0 < lim.max_single_instrument_exposure <= 1:
            raise ValueError("exposure limits must be in (0, 1] (no leverage)")
        if lim.max_position_notional is not None and lim.max_position_notional <= 0:
            raise ValueError("max_position_notional must be > 0")
        if not 0 <= self.rebalance_band < 1:
            raise ValueError("rebalance_band must be in [0, 1)")

    def fingerprint(self) -> str:
        payload = json.dumps({"risk": RISK_VERSION, **asdict(self)}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# Pre-declared research scenarios. The control (no overlay) is always first.
SCENARIOS: tuple[RiskConfig, ...] = (
    RiskConfig("control_no_overlay"),
    RiskConfig("fixed_fraction_50", sizing=SizingSpec("fixed_fraction", fraction=0.5)),
    RiskConfig(
        "volatility_target_10", sizing=SizingSpec("volatility_target", target_volatility=0.10)
    ),
    RiskConfig("drawdown_lock_20", drawdown=DrawdownSpec(enabled=True)),
    RiskConfig("atr_stop_3", stop=StopSpec("atr", atr_multiple=3.0)),
    RiskConfig(
        "atr_risk_1pct", sizing=SizingSpec("atr_risk", atr_risk_budget=0.01, atr_stop_multiple=2.0)
    ),
)
