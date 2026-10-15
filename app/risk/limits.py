"""Drawdown and session-loss monitors (state machines updated at each close) and exposure caps."""

import math
from dataclasses import dataclass

from app.risk.config import DrawdownSpec, LimitSpec, SessionSpec

NORMAL, WARNING, LOCKED = "NORMAL", "WARNING", "LOCKED"


def check_equity(equity: float) -> None:
    """ADR-0034 D-A5: a non-finite or negative equity is an invalid financial state and fails
    explicitly (never a valid-looking result). Equity equal to zero is valid."""
    if not (math.isfinite(equity) and equity >= 0):
        raise ValueError(f"invalid equity {equity!r}: must be finite and >= 0")


@dataclass
class DrawdownMonitor:
    spec: DrawdownSpec
    hwm: float
    max_drawdown: float = 0.0
    duration: int = 0  # consecutive closes below the high-water mark
    lock_remaining: int = 0
    state: str = NORMAL

    def update(self, equity: float) -> float:
        """Update at the close with the equity known at that close; returns current drawdown.
        Invalid state (D-A5) raises ValueError: non-finite or negative equity, non-finite or
        non-positive high-water mark. Equity 0 is valid and locks (0 <= lock level).

        ADR-0034 D-A2: the state is decided by comparing the equity with the LEVEL, computed in
        float64 — LOCKED iff equity <= hwm * (1 - lock), WARNING iff equity <= hwm * (1 -
        warning) — never by the ratio, which may read -0.19999999999999996 at exactly 80%.
        Equality activates, the next representable value above does not; no tolerance."""
        check_equity(equity)
        if not (math.isfinite(self.hwm) and self.hwm > 0):
            raise ValueError(f"invalid high-water mark {self.hwm!r}: must be finite and > 0")
        self.hwm = max(self.hwm, equity)
        dd = equity / self.hwm - 1
        self.max_drawdown = min(self.max_drawdown, dd)
        self.duration = self.duration + 1 if dd < 0 else 0
        if not self.spec.enabled:
            self.state = NORMAL
        elif self.lock_remaining > 0:
            self.lock_remaining -= 1
            if self.lock_remaining > 0:
                self.state = LOCKED
            elif equity > 0:  # cooldown over: unlock, reset HWM to current equity
                self.hwm, self.state, dd = equity, NORMAL, 0.0
            else:
                # Cooldown over at equity 0 (ADR-0034 D-A5/D-A6): the HWM is NOT reset to 0 (the
                # last positive HWM is kept, so the drawdown stays -1) and this close is judged
                # by the threshold rule like any other: 0 <= lock level (lock < 1), a new lock.
                self.state, self.lock_remaining = LOCKED, self.spec.cooldown_sessions
        elif equity <= self.lock_level:
            self.state, self.lock_remaining = LOCKED, self.spec.cooldown_sessions
        elif equity <= self.warning_level:
            self.state = WARNING
        else:
            self.state = NORMAL
        return dd

    @property
    def lock_level(self) -> float:
        """Equity at or below which the account locks (ADR-0034 D-A2)."""
        return self.hwm * (1 - self.spec.lock)

    @property
    def warning_level(self) -> float:
        """Equity at or below which the account is in WARNING (ADR-0034 D-A2)."""
        return self.hwm * (1 - self.spec.warning)


@dataclass
class SessionMonitor:
    spec: SessionSpec
    last_equity: float
    lock_remaining: int = 0
    consecutive_losses: int = 0

    def update(self, equity: float) -> float | None:
        """Session loss at the close, or None when it is not computable (previous equity 0,
        D-A5): no division and no substitute number; the monitor then keeps its protection —
        an active lock is neither released nor shortened, and the session blocks increases as
        a breach would. Invalid equity (non-finite or negative) raises ValueError."""
        check_equity(equity)
        check_equity(self.last_equity)
        if self.last_equity == 0:
            self.last_equity = equity
            if self.spec.enabled:
                self.lock_remaining = max(self.lock_remaining, self.spec.lock_sessions)
            return None
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


MIN_NOTIONAL_FRACTION = 1e-12  # = manager.EPS: an exposure at or below it is no position


def check_position_notional(spec: LimitSpec, initial_equity: float) -> None:
    """Rejects a `max_position_notional` that could never hold a position: at or below
    1e-12 x the initial equity the capped exposure is at or below the "no position" threshold,
    so every entry would be refused and every holding cut to an exit without any cause of the
    ADR-0034 vocabulary. Raised before anything is simulated or traded."""
    cap = spec.max_position_notional
    if cap is not None and not cap / initial_equity > MIN_NOTIONAL_FRACTION:
        raise ValueError(
            f"max_position_notional {cap!r} is too small for the initial equity "
            f"{initial_equity!r}: it must exceed {MIN_NOTIONAL_FRACTION} x the equity"
        )


def cap_exposure(exposure: float, spec: LimitSpec, *, equity: float) -> tuple[float, str | None]:
    """Applies gross / single-instrument / notional caps; leverage is impossible (cap <= 1)."""
    capped = min(exposure, spec.max_gross_exposure, spec.max_single_instrument_exposure)
    if spec.max_position_notional is not None and equity > 0:
        capped = min(capped, spec.max_position_notional / equity)
    return capped, ("exposure_limit" if capped < exposure else None)
