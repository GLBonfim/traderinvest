"""Drawdown and session-loss monitors (state machines updated at each close) and exposure caps."""

from dataclasses import dataclass

from app.risk.config import DrawdownSpec, LimitSpec, SessionSpec

NORMAL, WARNING, LOCKED = "NORMAL", "WARNING", "LOCKED"


@dataclass
class DrawdownMonitor:
    spec: DrawdownSpec
    hwm: float
    max_drawdown: float = 0.0
    duration: int = 0  # consecutive closes below the high-water mark
    lock_remaining: int = 0
    state: str = NORMAL

    def update(self, equity: float) -> float:
        """Update at the close with the equity known at that close; returns current drawdown."""
        self.hwm = max(self.hwm, equity)
        dd = equity / self.hwm - 1
        self.max_drawdown = min(self.max_drawdown, dd)
        self.duration = self.duration + 1 if dd < 0 else 0
        if not self.spec.enabled:
            self.state = NORMAL
        elif self.lock_remaining > 0:
            self.lock_remaining -= 1
            if self.lock_remaining == 0:  # cooldown over: unlock, reset HWM to current equity
                self.hwm, self.state, dd = equity, NORMAL, 0.0
            else:
                self.state = LOCKED
        elif dd <= -self.spec.lock:
            self.state, self.lock_remaining = LOCKED, self.spec.cooldown_sessions
        elif dd <= -self.spec.warning:
            self.state = WARNING
        else:
            self.state = NORMAL
        return dd


@dataclass
class SessionMonitor:
    spec: SessionSpec
    last_equity: float
    lock_remaining: int = 0
    consecutive_losses: int = 0

    def update(self, equity: float) -> float:
        loss = equity / self.last_equity - 1
        self.last_equity = equity
        if self.lock_remaining > 0:
            self.lock_remaining -= 1
        if self.spec.enabled and loss <= -self.spec.max_session_loss:
            self.lock_remaining = max(self.lock_remaining, self.spec.lock_sessions)
        return loss

    def record_round_trip(self, net_pnl: float) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if net_pnl < 0 else 0
        limit = self.spec.max_consecutive_losses
        if self.spec.enabled and limit is not None and self.consecutive_losses >= limit:
            self.lock_remaining = max(self.lock_remaining, self.spec.consecutive_lock_sessions)

    @property
    def locked(self) -> bool:
        return self.lock_remaining > 0


def cap_exposure(exposure: float, spec: LimitSpec, *, equity: float) -> tuple[float, str | None]:
    """Applies gross / single-instrument / notional caps; leverage is impossible (cap <= 1)."""
    capped = min(exposure, spec.max_gross_exposure, spec.max_single_instrument_exposure)
    if spec.max_position_notional is not None and equity > 0:
        capped = min(capped, spec.max_position_notional / equity)
    return capped, ("exposure_limit" if capped < exposure else None)
